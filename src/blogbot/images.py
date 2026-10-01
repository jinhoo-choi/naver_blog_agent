"""Bounded API image jobs with durable per-image checkpoints."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

from openai import APIStatusError, OpenAI

from .core import PostDraft
from .inputs import ContentRequest


class ImagePending(RuntimeError):
    """An incomplete or uncertain image must not discard the approved text."""


def atomic_json(path: Path, value: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(temporary, path)


def image_prompt(post: PostDraft, section: str, *, thumbnail: bool = False) -> str:
    subject = re.sub(r'[#|*_]', ' ', section).strip()[:300]
    scene = ''
    figures = 'asymmetric round heads, simple stick/box bodies, tiny dot eyes, minimal expression and'
    if post.category == 'parenting':
        scene = '''For parenting about an infant: show one plainly recognizable awake baby with round baby
proportions, short limbs and a plain onesie, or a relevant simple object. Never substitute an
adult, a screen/tablet, office work or a generic checking scene for an infant topic.
No toys, pillows, blankets, cords or loose objects in or near an infant sleep space.
If an infant sleep scene is relevant, show the infant on their back in fitted pajamas on a firm flat mattress with only a fitted sheet. Never draw blankets, pillows, toys or loose bedding in the crib, including curved blanket-like covers. Do not imply a toy becomes automatically safe after a birthday.'''
    elif post.category == 'investment':
        figures = ''
        scene = '''Objects only: no people, children, infants, baby-care props or medical crosses.
Use simple inanimate objects from the article. No letters at all, including the letters AI.'''
        if 'AI' in post.title and not thumbnail:
            scene += '\nDraw a plain rectangular microchip protected by a simple plain umbrella, and nothing else.'
    elif post.category == 'exercise':
        scene = 'Use adults or exercise equipment only; match the actual movement and age in the article.'
    lettering = 'No labels, letters, numbers, speech bubbles or captions in the image.'
    emphasis = ''
    if thumbnail:
        opening = re.split(r'\n\s*\n|(?=^## )', post.body.strip(), maxsplit=1,
                           flags=re.MULTILINE)[0][:300]
        lettering = f'''Use only this exact Korean headline, split into at most two large bold lines:
{post.title}
No other text, numbers, labels or speech bubbles. Render the Korean spelling accurately.'''
        scene = scene.replace('No letters at all, including the letters AI.', '')
        emphasis = f'''This is the article's summary cover for a small mobile preview.
Verified opening answer: {opening}
Summarize that answer with one immediately recognizable subject and one meaningful visual cue.
Use a large central subject, strong black-line contrast and one bright flat accent color.
Keep headline and subject inside the central 80% so a square crop stays understandable.
Readable at 160 by 160 pixels; no dense checklist, collage or decorative filler.
The cover must promise only what the article supports, with no fear bait or invented facts.'''
    return f'''Create one simple MS Paint mouse-drawn doodle for this Korean blog section.
Article: {post.title}\nSection: {subject}
Category: {post.category}. The depicted age, activity and props must match this title and section.
{scene}
Keep the same deliberately rough MS Paint doodle style in every image:
pure white background, thin slightly wobbly black mouse-drawn lines, {figures}
generous whitespace. Flat shapes only; at most one {'bright' if thumbnail else 'muted'} accent color.
{emphasis}
Show one clear everyday moment with at most one person and two simple props.
Keep objects separate and grounded: each hand belongs to one arm, any held
object touches that hand, furniture has a continuous outline, and nothing
floats, merges, duplicates or passes through another object. If a scene would
need complex anatomy or spatial relationships, show a single simple object
instead. {lettering}
No polished vector style, photorealism, 3D, watercolor, gradients, logos or watermark.
No medical or exercise anatomy diagrams,
unsupported exercise technique, numeric charts, fabricated statistics or financial promises.
Show the actual topic, not a generic thinking/checking/preparation scene. Do not depict a real family.'''


def generate_images(settings, request: ContentRequest, post: PostDraft) -> PostDraft:
    if post.category == 'cooking':
        return replace(post, photos=request.photos)
    config = settings.config.get('images', {})
    count = int(config.get(post.category + '_count', 1))
    if post.category == 'investment' and len(post.body) >= int(
        config.get('investment_extended_min_chars', 2500)
    ):
        count = int(config.get('investment_extended_count', count))
    sections = re.findall(r'^##\s+(.+)$', post.body, re.MULTILINE)
    if len(sections) < max(1, count - 1):
        raise ValueError('Not enough sections to place images')
    folder = settings.artifact_dir / 'generated-images' / post.request_id
    folder.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=settings.openai_api_key,
                    timeout=float(config.get('timeout_seconds', 120)), max_retries=0)

    def one(index: int) -> dict:
        thumbnail = index == 0
        section = '글 전체 핵심 요약' if thumbnail else sections[
            round((index - 1) * (len(sections) - 1) / max(1, count - 2))]
        params = {'model': str(config.get('model', 'gpt-image-2.5-flare')),
                  'prompt': image_prompt(post, section, thumbnail=thumbnail),
                  'quality': str(config.get('quality', 'medium')),
                  'size': str(config.get('size', '1024x1024')),
                  'output_format': str(config.get('output_format', 'jpeg'))}
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        suffix = 'jpg' if params['output_format'] == 'jpeg' else params['output_format']
        path = folder / f'{key}.{suffix}'
        manifest = folder / f'{key}.json'
        state = json.loads(manifest.read_text()) if manifest.exists() else {}

        def checkpoint(**values):
            nonlocal state
            state = {**state, **values, 'updated_at': time.time()}
            atomic_json(manifest, state)

        # A completed file survives even if a process died before its final manifest write.
        if path.exists() and path.stat().st_size > 0:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if state.get('sha256') and digest != state['sha256']:
                raise ImagePending('Image checksum mismatch')
            checkpoint(state='READY', sha256=digest)
            return {'file': str(path.resolve()), 'sha256': digest, 'caption': '',
                    'generated': True, 'cache_key': key, 'policy_version': 'category-scene-v2',
                    'role': 'thumbnail' if thumbnail else 'section'}
        attempts = int(state.get('attempts', 0))
        maximum = min(2, int(config.get('max_attempts', 2)))
        if state.get('recovery_attempted'):
            raise ImagePending('Image recovery budget reached')
        if state.get('state') in {'STARTED', 'UNCERTAIN', 'FAILED'} or attempts >= maximum:
            updated = state.get('updated_at')
            if not isinstance(updated, (int, float)) or not 0 < updated <= time.time():
                # Legacy checkpoints have no reliable age after artifact extraction.
                checkpoint()
                raise ImagePending('Image cooldown starts at first observed checkpoint')
            if time.time() - updated < 6 * 60 * 60:
                raise ImagePending('Image recovery cooldown has not elapsed')
            # Persist the one-call recovery budget before submitting another paid request.
            checkpoint(recovery_attempted=True)
            maximum = attempts + 1
        while attempts < maximum:
            attempts += 1
            started = time.monotonic()
            checkpoint(state='STARTED', attempts=attempts, cache_key=key)
            try:
                result = client.images.generate(**params)
                encoded = result.data[0].b64_json
                if not encoded:
                    raise ImagePending('Image response empty')
                data = base64.b64decode(encoded, validate=True)
                if not data:
                    raise ImagePending('Image response empty')
                temporary = path.with_suffix(path.suffix + '.tmp')
                temporary.write_bytes(data)
                os.replace(temporary, path)
                digest = hashlib.sha256(data).hexdigest()
                usage = getattr(result, 'usage', None)
                checkpoint(state='READY', attempts=attempts, sha256=digest,
                    elapsed_seconds=round(time.monotonic()-started, 2),
                    request_id=getattr(result, '_request_id', None),
                    usage=usage.model_dump() if usage else None)
                return {'file': str(path.resolve()), 'sha256': digest, 'caption': '',
                        'generated': True, 'cache_key': key, 'policy_version': 'category-scene-v2',
                        'role': 'thumbnail' if thumbnail else 'section'}
            except APIStatusError as exc:
                # Only explicit rate-limit rejection is safe for one bounded retry.
                retryable = exc.status_code == 429
                checkpoint(state='RATE_LIMITED' if retryable else 'FAILED',
                           attempts=attempts, http_status=exc.status_code)
                if retryable and attempts < maximum:
                    time.sleep(10)
                    continue
                raise ImagePending('Image API request not completed') from None
            except Exception as exc:  # noqa: BLE001 -- Any post-submission failure is uncertain.
                checkpoint(state='UNCERTAIN', attempts=attempts, error=type(exc).__name__)
                raise ImagePending('Image API outcome uncertain') from None
        raise ImagePending('Image retry budget reached')

    with ThreadPoolExecutor(max_workers=min(2, max(1, int(config.get('concurrency', 2))))) as pool:
        futures = [pool.submit(one, i) for i in range(count)]
        photos, errors = [], []
        for future in futures:
            try:
                photos.append(future.result())
            except Exception as exc:  # noqa: BLE001 -- Collect worker failures, then raise below.
                errors.append(exc)
        if errors:
            raise ImagePending('Some images are pending; completed files retained') from errors[0]
    return replace(post, photos=photos)

