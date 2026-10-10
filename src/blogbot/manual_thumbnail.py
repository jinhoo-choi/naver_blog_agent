"""One explicit origins cover brief through the existing image engine, never prose approval."""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
from pathlib import Path

from cryptography.fernet import Fernet
from PIL import Image

from .core import PostDraft, today_kst
from .images import execute_image_plans, image_prompt
from .inputs import verify_photos
from .manual_costs import estimate_project, usage_cost, validate_budget
from .manual_requests import (
    SLUG,
    _existing,
    _run_identity,
    _text,
    _workflow_outputs,
    atomic_json,
    check_remote_history,
    reservation_name,
    verify_remote_reservation,
)
from .planning import media_claim


def validate_thumbnail_packet(settings, ciphertext, request_id, packet_sha256):
    if (not isinstance(request_id, str) or not re.fullmatch(SLUG, request_id)
            or not isinstance(packet_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', packet_sha256)
            or not isinstance(ciphertext, str) or not 0 < len(ciphertext) <= 30_000):
        raise ValueError('Invalid explicit thumbnail selector')
    raw = Fernet(os.environ['BLOG_BUNDLE_KEY'].encode()).decrypt(ciphertext.encode())
    if hashlib.sha256(raw).hexdigest() != packet_sha256:
        raise ValueError('Thumbnail approval digest mismatch')
    packet = json.loads(raw)
    fields = {'version', 'request_id', 'date', 'approval_reference', 'approved_by', 'scope',
              'category', 'question', 'title', 'summary', 'sources', 'budget'}
    if (not isinstance(packet, dict) or set(packet) != fields
            or packet['version'] != 'manual-thumbnail-v1' or packet['request_id'] != request_id
            or packet['date'] != str(today_kst()) or packet['scope'] != 'thumbnail_only'
            or packet['category'] != 'origins' or packet['approved_by'] != 'repository_owner'
            or not isinstance(packet['approval_reference'], str)
            or not re.fullmatch(r'Sentinel_[A-Za-z0-9_-]{8,100}', packet['approval_reference'])
            or not _text(packet['question'], 500) or not _text(packet['title'], 80)
            or not _text(packet['summary'], 600)
            or not isinstance(packet['sources'], list) or not 1 <= len(packet['sources']) <= 3):
        raise ValueError('Explicit current thumbnail brief approval required')
    from urllib.parse import urlsplit
    for source in packet['sources']:
        if (not isinstance(source, dict) or set(source) != {'url', 'excerpt'}
                or not _text(source['url'], 2000) or not _text(source['excerpt'], 2000)):
            raise ValueError('Verified thumbnail source required')
        url = urlsplit(source['url'])
        if url.scheme != 'https' or not url.hostname or url.username or url.password:
            raise ValueError('Invalid thumbnail source URL')
    validate_budget(packet['budget'], settings)
    image_config = settings.config['images']
    required = {'model': 'gpt-image-2.5-flare', 'quality': 'medium', 'size': '1024x1024',
                'output_format': 'jpeg'}
    if any(image_config.get(key) != value for key, value in required.items()):
        raise ValueError('Existing image model/configuration drift; no substitution')
    if settings.config['categories']['origins'].get('naver_category_no') != 9:
        raise ValueError('Origins category configuration unavailable')
    estimate_project(packet['budget'], thumbnail_params(settings, packet)['prompt'])
    return packet


def thumbnail_params(settings, packet):
    post = PostDraft(category='origins', subcategory='이름의 유래', topic=packet['question'],
                     title=packet['title'], body=packet['summary'], tags=[], source_urls=[],
                     as_of_date=packet['date'], request_id=packet['request_id'],
                     status='THUMBNAIL_BRIEF')
    return {**{key: settings.config['images'][key] for key in
               ('model', 'quality', 'size', 'output_format')},
            'prompt': image_prompt(post, '글 전체 핵심 요약', thumbnail=True), 'n': 1}


def _verify_result(directory, claim):
    result = json.loads((directory / 'result.json').read_text())
    identity = {key: value for key, value in result.items() if key != 'photo'}
    identity['photo'] = {k: v for k, v in result['photo'].items() if k != 'file'}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    if digest != claim.get('result_sha256'):
        raise ValueError('Prepared thumbnail result integrity mismatch')
    verify_photos([result['photo']])
    return result


def reserve_thumbnail(settings, ciphertext, request_id, packet_sha256):
    packet = validate_thumbnail_packet(settings, ciphertext, request_id, packet_sha256)
    directory = settings.db_path.parent / 'manual-thumbnails' / request_id
    with media_claim(settings.db_path.parent, 'manual-requests'):
        _existing(settings, request_id)
        if directory.exists():
            claim = json.loads((directory / 'claim.json').read_text())
            if claim.get('packet_sha256') == packet_sha256 and claim.get('status') == 'MANUAL_THUMBNAIL_READY':
                _verify_result(directory, claim)
                _workflow_outputs(cached=True)
                return {'request_id': request_id, 'status': 'MANUAL_THUMBNAIL_READY', 'cached': True}
            raise ValueError('Thumbnail already reserved; no new ID/date retry')
        if (settings.db_path.parent / 'manual-requests' / request_id).exists():
            raise ValueError('Text-stage history forbids a later paid thumbnail')
        question_hash = hashlib.sha256(re.sub(r'\s+', '', packet['question']).casefold().encode()).hexdigest()
        for other in directory.parent.glob('*/claim.json'):
            previous = json.loads(other.read_text())
            if (previous.get('approval_reference') == packet['approval_reference']
                    or previous.get('question_sha256') == question_hash):
                raise ValueError('Existing thumbnail approval/question cannot use a new ID')
        run_id = _run_identity()
        check_remote_history(packet, 'manual')
        check_remote_history(packet, 'thumbnail')
        directory.mkdir(parents=True, exist_ok=False)
        claim = {'request_id': request_id, 'date': packet['date'], 'packet_sha256': packet_sha256,
                 'approval_reference': packet['approval_reference'], 'run_id': run_id,
                 'status': 'RESERVED', 'budget': packet['budget'], 'question_sha256': question_hash,
                 'cost_estimate': estimate_project(packet['budget'], thumbnail_params(settings, packet)['prompt'])}
        atomic_json(directory / 'claim.json', claim)
        atomic_json(directory / 'approved-packet.json', packet)
        _workflow_outputs(cached=False, artifact_name=reservation_name(run_id, packet, 'thumbnail'))
        return {'request_id': request_id, 'status': 'MANUAL_THUMBNAIL_RESERVED'}


def run_thumbnail(settings, ciphertext, request_id, packet_sha256):
    packet = validate_thumbnail_packet(settings, ciphertext, request_id, packet_sha256)
    directory = settings.db_path.parent / 'manual-thumbnails' / request_id
    with media_claim(settings.db_path.parent, 'manual-requests'):
        _existing(settings, request_id)
        claim_path = directory / 'claim.json'
        claim = json.loads(claim_path.read_text())
        if claim.get('packet_sha256') != packet_sha256:
            raise ValueError('Changed thumbnail approval')
        if claim['status'] == 'MANUAL_THUMBNAIL_READY':
            _verify_result(directory, claim)
            return {'request_id': request_id, 'status': 'MANUAL_THUMBNAIL_READY', 'cached': True}
        if claim['status'] != 'RESERVED' or claim['run_id'] != _run_identity():
            return {'request_id': request_id, 'status': 'MANUAL_CHECK_REQUIRED'}
        verify_remote_reservation(claim['run_id'], packet, 'thumbnail')
        claim['status'] = 'STARTED'
        atomic_json(claim_path, claim)
        params = thumbnail_params(settings, packet)
        key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
        def before_submit(actual):
            if packet['date'] != str(today_kst()):
                raise ValueError('Dated thumbnail approval crossed midnight')
            if actual != params or (directory / 'paid-call.json').exists():
                raise ValueError('Exactly one pinned thumbnail submission is allowed')
            atomic_json(directory / 'paid-call.json', {'status': 'STARTED', 'actual_call_date_kst': str(today_kst()),
                                                       'plan_sha256': key,
                                                       'cost_estimate': claim['cost_estimate']})
        try:
            photos = execute_image_plans(settings, directory / 'image', [(params, key)], [{}],
                                         allow_uncertain_recovery=False, before_submit=before_submit,
                                         max_attempts=1)
            data = Path(photos[0]['file']).read_bytes()
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
            manifest = json.loads((directory / 'image' / (key + '.json')).read_text())
            cost = usage_cost({'usage': manifest.get('usage')}, image=True)
            cost_basis = 'reported_usage' if cost is not None else 'preflight_estimate_usage_unavailable'
            if cost is None:
                cost = claim['cost_estimate']['image_scenario_usd']
            result = {'accounted_cost_usd': str(cost), 'cost_basis': cost_basis,
                      'request_id': request_id, 'date': packet['date'],
                      'completed_date_kst': str(today_kst()), 'photo': photos[0],
                      'title': packet['title'], 'status': 'MANUAL_THUMBNAIL_READY',
                      'requires_visual_review': True, 'budget': packet['budget']}
            identity = {**result, 'photo': {k: v for k, v in photos[0].items() if k != 'file'}}
            claim['result_sha256'] = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
            atomic_json(directory / 'result.json', result)
            claim['status'] = 'MANUAL_THUMBNAIL_READY'
            atomic_json(claim_path, claim)
            return {'request_id': request_id, 'status': 'MANUAL_THUMBNAIL_READY',
                    'requires_visual_review': True, 'cached': False}
        except Exception:
            claim['status'] = 'HELD'
            atomic_json(claim_path, claim)
            raise
