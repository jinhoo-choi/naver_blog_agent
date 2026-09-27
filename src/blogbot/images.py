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


def image_prompt(post: PostDraft, section: str) -> str:
    subject = re.sub(r'[#|*_]', ' ', section).strip()[:300]
    return f'''Create one simple MS Paint mouse-drawn doodle for this Korean blog section.
Article: {post.title}\nSection: {subject}
White background, thin slightly wobbly black lines, asymmetric round heads,
simple stick/box bodies, minimal facial expression, generous whitespace.
One relatable situation or simple concept; optional single accent color.
At most one short Korean note of 2-8 characters; omit text if unnecessary.
No polished vector style, photorealism, 3D, watercolor, gradients, logos or watermark.
No medical or exercise anatomy diagrams, hazardous infant sleep arrangements,
unsupported exercise technique, numeric charts, fabricated statistics or financial promises.
Prefer a generic thinking/checking/preparation scene. Do not depict a real family.'''


def generate_images(settings, request: ContentRequest, post: PostDraft) -> PostDraft:
    if post.category == 'cooking':
        return replace(post, photos=request.photos)
    config = settings.config.get('images', {})
    count = int(config.get(post.category + '_count', 1))
    sections = re.findall(r'^##\s+(.+)$', post.body, re.MULTILINE)
    if len(sections) < count:
        raise ValueError('Not enough sections to place images')
    folder = settings.artifact_dir / 'generated-images' / post.request_id
    folder.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=settings.openai_api_key,
                    timeout=float(config.get('timeout_seconds', 120)), max_retries=0)

    def one(index: int) -> dict:
        section = sections[min(len(sections)-1, (index+1)*len(sections)//(count+1))]
        params = {'model': str(config.get('model', 'gpt-image-2.5-flare')),
                  'prompt': image_prompt(post, section), 'quality': str(config.get('quality', 'low')),
                  'size': str(config.get('size', '1024x1024')),
                  'output_format': str(config.get('output_format', 'jpeg'))}
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        suffix = 'jpg' if params['output_format'] == 'jpeg' else params['output_format']
        path = folder / f'{key}.{suffix}'
        manifest = folder / f'{key}.json'
        state = json.loads(manifest.read_text()) if manifest.exists() else {}
        # A completed file survives even if a process died before its final manifest write.
        if path.exists() and path.stat().st_size > 0:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if state.get('sha256') and digest != state['sha256']:
                raise ImagePending('Image checksum mismatch')
            atomic_json(manifest, {**state, 'state': 'READY', 'sha256': digest})
            return {'file': str(path.resolve()), 'sha256': digest, 'caption': '',
                    'generated': True, 'cache_key': key}
        if state.get('state') in {'STARTED', 'UNCERTAIN', 'FAILED'}:
            raise ImagePending('Uncertain image request requires reconciliation')
        attempts = int(state.get('attempts', 0))
        maximum = min(2, int(config.get('max_attempts', 2)))
        while attempts < maximum:
            attempts += 1
            started = time.monotonic()
            atomic_json(manifest, {'state': 'STARTED', 'attempts': attempts, 'cache_key': key})
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
                atomic_json(manifest, {'state': 'READY', 'attempts': attempts, 'sha256': digest,
                    'elapsed_seconds': round(time.monotonic()-started, 2),
                    'request_id': getattr(result, '_request_id', None),
                    'usage': usage.model_dump() if usage else None})
                return {'file': str(path.resolve()), 'sha256': digest, 'caption': '',
                        'generated': True, 'cache_key': key}
            except APIStatusError as exc:
                # Only explicit rate-limit rejection is safe for one bounded retry.
                retryable = exc.status_code == 429
                atomic_json(manifest, {'state': 'RATE_LIMITED' if retryable else 'FAILED',
                                      'attempts': attempts, 'http_status': exc.status_code})
                if retryable and attempts < maximum:
                    time.sleep(10)
                    continue
                raise ImagePending('Image API request not completed') from None
            except Exception as exc:  # noqa: BLE001 -- Any post-submission failure is uncertain.
                atomic_json(manifest, {'state': 'UNCERTAIN', 'attempts': attempts,
                                      'error': type(exc).__name__})
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
