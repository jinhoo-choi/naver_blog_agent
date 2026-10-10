"""One explicitly approved manual manuscript, images only; never a publishing gate."""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict

from cryptography.fernet import Fernet

from .core import PostDraft
from .images import ImagePending, atomic_json, generate_images, section_contexts
from .inputs import ContentRequest
from .planning import media_claim

# Pin the reviewed private packet without putting manuscript text in the repository.
REQUEST_ID = 'owner-20261009-new-policy'
PACKET_SHA256 = 'abb15bcf071f196821c308065248ee136a44c6e5117a512808c5dc7e06c041e1'
BODY_SHA256 = '589de25a8a0a273d66703bd0e085bacd34f25c5eca6c02ed50b989b30742e494'


def approved_packet(ciphertext: str, request_id: str) -> dict:
    if request_id != REQUEST_ID or not 0 < len(ciphertext) <= 40_000:
        raise ValueError('Manual image selector or encrypted input is invalid')
    raw = Fernet(os.environ['BLOG_BUNDLE_KEY'].encode()).decrypt(ciphertext.encode())
    if hashlib.sha256(raw).hexdigest() != PACKET_SHA256:
        raise ValueError('Manual image packet is not the reviewed version')
    packet = json.loads(raw)
    review = packet.get('text_review', {})
    if (packet.get('request_id') != REQUEST_ID
            or packet.get('category') != 'investment'
            or packet.get('as_of_date') != '2026-10-09'
            or packet.get('status') != 'LOCAL_TEXT_REVIEWED'
            or packet.get('manual_owner_replacement') is not True
            or packet.get('published') is not False
            or review.get('reviewer') != 'independent'
            or review.get('accuracy', 0) < 4 or review.get('safety', 0) < 4
            or hashlib.sha256(packet['body'].encode()).hexdigest() != BODY_SHA256):
        raise ValueError('Manual image approval evidence does not match')
    return packet


def run_manual_images(settings, ciphertext: str, request_id: str) -> dict:
    packet = approved_packet(ciphertext, request_id)
    # Preserve the existing model, API key, per-image budgets and image ceilings.
    # Refuse configuration drift instead of overriding configuration for a paid call.
    config = settings.config.get('images', {})
    if (len(packet['body']) >= int(config.get('investment_extended_min_chars', 2500))
            or not 1 <= int(config.get('investment_count', 1)) <= packet['image_ceiling']):
        raise ValueError('Manual image ceiling needs reconciliation')
    request = ContentRequest(REQUEST_ID, packet['category'], {})
    post = PostDraft(
        category=packet['category'], subcategory=packet['subcategory'], topic=packet['title'],
        title=packet['title'], body=packet['body'], tags=packet['tags'],
        source_urls=packet['source_urls'], as_of_date=packet['as_of_date'],
        status=packet['status'], request_id=REQUEST_ID,
        provenance={'manual_image_only': True, 'approved_packet_sha256': PACKET_SHA256},
    )
    # Exact pinned packet: company explanations are sections 2 and 3. Never use
    # policy/market sections or accept free-form image prompts from workflow input.
    sections = section_contexts(post.body)
    if len(sections) != 4:
        raise ValueError('Manual image sections need reconciliation')
    headings = [sections[index].splitlines()[0] for index in (1, 2)]
    target = settings.artifact_dir / f'manual-images-{REQUEST_ID}.json'
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    with media_claim(settings.db_path.parent, REQUEST_ID):
        payload = {'post': asdict(post), 'input': asdict(request),
                   'review': packet['text_review'], 'approved_packet': packet,
                   'media_status': 'IMAGES_PENDING'}
        if target.exists():
            previous = json.loads(target.read_text())
            if previous.get('approved_packet') != packet:
                raise ValueError('Existing manual image packet conflicts; preserve checkpoints')
        # Persist before any submission so pack retains partial assets/manifests on error.
        atomic_json(target, payload)
        try:
            post = generate_images(settings, request, post, allow_uncertain_recovery=False,
                                   section_headings=headings)
        except ImagePending:
            return {'request_id': REQUEST_ID, 'status': 'IMAGES_PENDING'}
        payload.update(post=asdict(post), media_status='MANUAL_IMAGES_READY')
        atomic_json(target, payload)
    # Deliberately no posts/attempts/save_receipts update, no APPROVED or ready.json entry.
    return {'request_id': REQUEST_ID, 'status': 'MANUAL_IMAGES_READY',
            'images': len(post.photos), 'requires_visual_review': True}
