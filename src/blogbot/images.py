"""Generate bright, topic-matched article illustrations with the Images API."""
from __future__ import annotations

import base64
import hashlib
import re
from dataclasses import replace

from openai import OpenAI

from .core import PostDraft
from .inputs import ContentRequest


def image_prompt(post: PostDraft, section: str) -> str:
    subject = re.sub(r"[#|*_]", " ", section).strip()[:300]
    category = "Korean parenting" if post.category == "parenting" else "Korean investing study"
    return f"""Use case: illustration-story
Asset type: mid-article image for a {category} blog
Primary request: Create a bright, warm editorial illustration matching this section: {subject}
Article title: {post.title}
Style: polished soft gouache and digital magazine illustration, natural proportions
Composition: landscape 3:2, medium-wide, uncluttered, useful between article sections
Lighting: bright diffuse morning daylight
Palette: ivory, pale mint, warm apricot, light wood
Constraints: generic fictional people when people are useful; no identifiable person; no logos; no text;
no watermark; no medical diagnosis; no product endorsement; no investment recommendation
Avoid: dark shadows, gloomy colors, clutter, charts with invented numbers, brand marks"""


def generate_images(settings, request: ContentRequest, post: PostDraft) -> PostDraft:
    if post.category == "cooking":
        return replace(post, photos=request.photos)
    config = settings.config.get("images", {})
    if config.get("mode", "generate") != "generate":
        return replace(post, photos=request.photos)
    count = int(config.get("parenting_count", 2) if post.category == "parenting"
                else config.get("investment_count", 1))
    sections = re.findall(r"^##\s+(.+)$", post.body, re.MULTILINE)
    if len(sections) < count:
        raise ValueError("Not enough sections to place generated images")
    client = OpenAI(api_key=settings.openai_api_key, timeout=240, max_retries=1)
    folder = settings.artifact_dir / "generated-images" / post.request_id
    folder.mkdir(parents=True, exist_ok=True)
    photos = []
    for i in range(count):
        section = sections[min(len(sections) - 1, (i + 1) * len(sections) // (count + 1))]
        result = client.images.generate(
            model=str(config.get("model", "gpt-image-2.5-flare")),
            prompt=image_prompt(post, section), quality=str(config.get("quality", "low")),
            size=str(config.get("size", "1536x1024")), output_format="png",
        )
        encoded = result.data[0].b64_json
        if not encoded:
            raise RuntimeError("Image API returned no image data")
        data = base64.b64decode(encoded, validate=True)
        path = folder / f"section-{i + 1}.png"
        path.write_bytes(data)
        photos.append({
            "file": str(path.resolve()), "sha256": hashlib.sha256(data).hexdigest(),
            "caption": f"‘{section}’ 내용을 밝은 육아·생활 일러스트로 표현했습니다. "
                       "실제 인물·상황을 재현한 사진이 아닙니다.",
            "author": "OpenAI image generation", "license": "AI-generated illustration",
            "generated": True,
        })
    return replace(post, photos=photos)
