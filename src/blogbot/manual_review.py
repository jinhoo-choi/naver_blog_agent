"""One separately authorized source review of an immutable held manual manuscript."""
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from .core import KST, max_title_similarity, review_result, today_kst
from .inputs import ContentRequest, verify_photos
from .llm import (
    BlogLLM,
    _cached_review_request,
    _checked_review,
    _extract_urls,
    _historical_source_urls,
    _source_identity,
)
from .manual_costs import thumbnail_spend, validate_budget
from .manual_requests import (
    SLUG,
    BudgetedResponses,
    _existing,
    _result_digest,
    _run_identity,
    _workflow_outputs,
    atomic_json,
    check_remote_history,
    reservation_name,
    verify_remote_reservation,
)
from .planning import media_claim
from .pre_review import DraftCandidate, inspect_candidate
from .responses import DRAFT_SCHEMA

VERSION = 'manual-review-v1'
SOURCE_GATE = '핵심 주장별 원문 열람·근거 대조가 완료되지 않았습니다.'
FIELDS = {'version', 'request_id', 'date', 'approval_reference', 'original_approval_reference',
          'approved_by', 'scope', 'question', 'original_claim_sha256', 'original_calls_sha256',
          'cache_file', 'writer_payload_sha256', 'writer_response_id', 'evidence', 'budget'}


def _digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _sha(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r'[a-f0-9]{64}', value))


def _approval(value) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r'Sentinel_[A-Za-z0-9_-]{8,100}', value))


def _normalized(text: str) -> str:
    return re.sub(r'\s+', '', text)


def _evidence(packet, original):
    supplied = packet['evidence']
    if not isinstance(supplied, list) or not 1 <= len(supplied) <= 3:
        raise ValueError('One to three verified original-source excerpts are required')
    sources = {_source_identity(item['url']): item['excerpt'] for item in original['sources']}
    evidence, seen = [], set()
    for item in supplied:
        if (not isinstance(item, dict)
                or set(item) != {'url', 'text', 'sha256', 'retrieved_at', 'verified_by'}
                or not isinstance(item['url'], str) or len(item['url']) > 2000
                or not isinstance(item['text'], str) or not item['text'].strip()
                or len(item['text']) > 12000 or not _sha(item['sha256'])
                or hashlib.sha256(item['text'].encode()).hexdigest() != item['sha256']
                or item['verified_by'] not in {'operator', 'repository_owner'}):
            raise ValueError('Invalid verified review source evidence')
        url = urlsplit(item['url'])
        if (url.scheme != 'https' or not url.hostname
                or (url.hostname != 'korean.go.kr' and not url.hostname.endswith('.korean.go.kr'))
                or url.username or url.password or url.port or any(c.isspace() for c in item['url'])):
            raise ValueError('Review evidence must identify an approved official Korean dictionary URL')
        identity = _source_identity(item['url'])
        if (identity not in sources or identity in seen
                or _normalized(sources[identity]) not in _normalized(item['text'])):
            raise ValueError('Review evidence must retain the original exact source and excerpt')
        try:
            stamp = datetime.fromisoformat(item['retrieved_at'])
        except (TypeError, ValueError):
            raise ValueError('Verified evidence needs its actual retrieval timestamp') from None
        if (stamp.tzinfo is None or stamp.astimezone(KST).date() != today_kst()
                or stamp > datetime.now(KST)):
            raise ValueError('Review evidence retrieval date is not current or is in the future')
        seen.add(identity)
        evidence.append({**item, 'retrieved_date': stamp.astimezone(KST).date().isoformat()})
    if seen != set(sources):
        raise ValueError('Review requires verified excerpts for every original source')
    return evidence


def _cache(directory, filename):
    if (not isinstance(filename, str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,180}\.json', filename)):
        raise ValueError('An exact existing response-cache basename is required')
    path = directory / 'response-cache' / filename
    if not path.resolve().is_relative_to((directory / 'response-cache').resolve()):
        raise ValueError('Review cache path escapes its original namespace')
    return json.loads(path.read_text())


def _original_state(settings, packet):
    root = settings.db_path.parent
    folder = root / 'manual-requests' / packet['request_id']
    prior = folder / 'review-once' / 'original-claim.json'
    raw_claim = prior.read_bytes() if prior.exists() else (folder / 'claim.json').read_bytes()
    raw_calls = (folder / 'paid-calls.json').read_bytes()
    if (hashlib.sha256(raw_claim).hexdigest() != packet['original_claim_sha256']
            or hashlib.sha256(raw_calls).hexdigest() != packet['original_calls_sha256']):
        raise ValueError('Original held claim or four-call history changed')
    claim, calls = json.loads(raw_claim), json.loads(raw_calls)
    original = json.loads((folder / 'approved-packet.json').read_text())
    if (claim.get('status') != 'HELD' or claim.get('request_id') != packet['request_id']
            or claim.get('scope') != 'draft_only'
            or claim.get('approval_reference') != packet['original_approval_reference']
            or original.get('request_id') != packet['request_id'] or original.get('category') != 'origins'
            or original.get('version') != 'manual-request-v1' or original.get('scope') != 'draft_only'
            or original.get('approved_by') != 'repository_owner'
            or original.get('approval_reference') != packet['original_approval_reference']
            or original.get('question') != packet['question'] or original.get('budget') != packet['budget']
            or claim.get('budget') != packet['budget']
            or packet['approval_reference'] in {
                original['approval_reference'], packet['budget']['cost_approval_reference']}):
        raise ValueError('Review exception requires a separate approval for the same held manuscript')
    if (not isinstance(calls, list) or len(calls) != 4
            or [c.get('ordinal') for c in calls] != [1, 2, 3, 4]
            or any(c.get('status') != 'RETURNED' for c in calls)
            or [c.get('max_output_tokens') for c in calls] != [12000, 6000, 12000, 6000]
            or any(c.get('model') != 'gpt-5' for c in calls)):
        raise ValueError('Exactly four completed original writer/review calls are required')
    history = [json.loads(line) for line in (root / 'usage.jsonl').read_text().splitlines()
               if line.strip()]
    history = [row for row in history if row.get('request_id') == packet['request_id']]
    if (len(history) != 4
            or [row.get('stage') for row in history] != ['writer', 'reviewer', 'rewrite', 'reviewer']
            or any(row.get('error') or row.get('status') != 'completed'
                   or not row.get('response_id') for row in history)
            or history[2]['response_id'] != packet['writer_response_id']):
        raise ValueError('Original response journal does not prove the completed final rewrite')
    cached = _cache(root, packet['cache_file'])
    if (cached.get('response', {}).get('id') != packet['writer_response_id']
            or cached['response'].get('status') != 'completed'
            or not isinstance(cached.get('payload'), dict)
            or _digest(cached['payload']) != packet['writer_payload_sha256']):
        raise ValueError('Final rewrite payload or response identity does not match approval')
    review_caches = []
    for path in (root / 'response-cache').glob('*.json'):
        item = json.loads(path.read_text())
        if item.get('response', {}).get('id') == history[-1]['response_id']:
            review_caches.append(item)
    if len(review_caches) != 1:
        raise ValueError('One exact final original review response is required')
    previous = review_caches[0]
    if previous['response'].get('status') != 'completed':
        raise ValueError('The previous review outcome is not completed')
    score, decision = review_result(previous.get('payload', {}))
    if decision != 'PASS' or score < settings.config['blog']['review_pass_score']:
        raise ValueError('Only a previous source-gate-only rejection is eligible')
    original_request = ContentRequest(packet['request_id'], 'origins', {
        'question': original['question'], 'context': original['context'], 'sources': original['sources']},
        provenance={'manual_request': {'version': 'manual-request-v1', 'date': original['date'],
                                      'scope': 'draft_only'}})
    old_checked = _checked_review(previous['payload'], previous['response'],
                                  _cached_review_request(original_request, previous))
    if (old_checked.get('decision') != 'REWRITE'
            or old_checked.get('blocking_issues') != [SOURCE_GATE]):
        raise ValueError('The held outcome is not solely the missing source-reading gate')
    photo = original['thumbnail']
    if 'prepared_request_id' in photo:
        from .manual_thumbnail import _verify_result
        thumb_root = root / 'manual-thumbnails' / packet['request_id']
        prepared = _verify_result(thumb_root, json.loads((thumb_root / 'claim.json').read_text()))
        extension = Path(prepared['photo']['file']).suffix
    else:
        extension = '.' + photo['extension']
    image_path = folder / ('thumbnail' + extension)
    photos = [{key: photo[key] for key in ('sha256', 'approved', 'generated', 'role', 'caption')}]
    photos[0]['file'] = str(image_path.resolve())
    verify_photos(photos)
    evidence = _evidence(packet, original)
    request = ContentRequest(packet['request_id'], 'origins', original_request.data, photos,
                             {**original_request.provenance, 'reference_evidence': evidence,
                              'manual_review': {'version': VERSION,
                                                'approval_reference': packet['approval_reference']}})
    post, issues, changes = inspect_candidate(
        DraftCandidate(cached['payload'], _extract_urls(cached['response'])
                       + _historical_source_urls(root / 'usage.jsonl', packet['request_id'])), request,
        settings.config['categories']['origins'],
        settings.config.get('editorial', {}).get('require_structure', True))
    if (issues or changes or post is None
            or any(getattr(post, key) != cached['payload'][key] for key in DRAFT_SCHEMA['required'])):
        raise ValueError('Pinned rewrite cannot be changed or repaired during review-only recovery')
    spent = thumbnail_spend(settings, packet['request_id'])
    for call in calls:
        cost = Decimal(call['accounted_cost_usd'])
        if not cost.is_finite() or cost < 0:
            raise ValueError('Invalid original accounted cost')
        spent += cost
    if spent + Decimal('0.59') > Decimal(str(packet['budget']['max_estimated_usd'])):
        raise ValueError('One additional review exceeds remaining approved estimated budget')
    return {'claim': claim, 'raw_claim': raw_claim, 'post': post, 'request': request,
            'spent': spent, 'original': original, 'calls': calls}


def validate_review_packet(settings, ciphertext, request_id, packet_sha256):
    if (not isinstance(request_id, str) or not re.fullmatch(SLUG, request_id)
            or not _sha(packet_sha256) or not isinstance(ciphertext, str)
            or not 0 < len(ciphertext) <= 65000):
        raise ValueError('Invalid explicit manual review selector')
    raw = Fernet(os.environ['BLOG_BUNDLE_KEY'].encode()).decrypt(ciphertext.encode())
    if len(raw) > 48000 or hashlib.sha256(raw).hexdigest() != packet_sha256:
        raise ValueError('Review approval packet digest mismatch')
    packet = json.loads(raw)
    if (not isinstance(packet, dict) or set(packet) != FIELDS or packet['version'] != VERSION
            or packet['request_id'] != request_id or packet['date'] != str(today_kst())
            or packet['scope'] != 'review_only' or packet['approved_by'] != 'repository_owner'
            or not _approval(packet['approval_reference'])
            or not _approval(packet['original_approval_reference'])
            or not all(_sha(packet[key]) for key in
                       ('original_claim_sha256', 'original_calls_sha256', 'writer_payload_sha256'))
            or not isinstance(packet['writer_response_id'], str)
            or not packet['writer_response_id'] or len(packet['writer_response_id']) > 150
            or not isinstance(packet['question'], str) or not 1 <= len(packet['question']) <= 500):
        raise ValueError('Explicit current owner approval for one existing review is required')
    validate_budget(packet['budget'], settings)
    _original_state(settings, packet)
    return packet


def _write_original(path, data):
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _cached(settings, folder, packet_sha256):
    claim = json.loads((folder / 'claim.json').read_text())
    if claim.get('packet_sha256') != packet_sha256:
        raise ValueError('Existing one-shot review approval cannot be replaced')
    if claim.get('status') == 'MANUAL_DRAFT_READY':
        top = json.loads((folder.parent / 'claim.json').read_text())
        result = json.loads((folder.parent / 'result.json').read_text())
        if (top.get('status') != 'MANUAL_DRAFT_READY'
                or top.get('result_sha256') != _result_digest(result)
                or top.get('review_recovery', {}).get('packet_sha256') != packet_sha256
                or claim.get('result_sha256') != top['result_sha256']):
            raise ValueError('Recovered manuscript result integrity needs reconciliation')
        verify_photos(result['post']['photos'])
        return {'request_id': claim['request_id'], 'status': 'MANUAL_DRAFT_READY', 'cached': True}
    return {'request_id': claim['request_id'], 'status': 'MANUAL_CHECK_REQUIRED'}


def reserve_review(settings, ciphertext, request_id, packet_sha256):
    packet = validate_review_packet(settings, ciphertext, request_id, packet_sha256)
    parent = settings.db_path.parent / 'manual-requests' / request_id
    folder = parent / 'review-once'
    with media_claim(settings.db_path.parent, 'manual-requests'):
        _existing(settings, request_id)
        if (parent / 'save-receipt.json').exists():
            raise ValueError('Existing manual save history forbids review re-delivery')
        if folder.exists():
            result = _cached(settings, folder, packet_sha256)
            if result['status'] != 'MANUAL_DRAFT_READY':
                raise ValueError('Manual review was already reserved; no new paid attempt')
            _workflow_outputs(cached=True)
            return result
        state = _original_state(settings, packet)
        run_id = _run_identity()
        check_remote_history(packet, 'review')
        folder.mkdir(parents=True, exist_ok=False)
        _write_original(folder / 'original-claim.json', state['raw_claim'])
        claim = {'request_id': request_id, 'date': packet['date'], 'packet_sha256': packet_sha256,
                 'approval_reference': packet['approval_reference'], 'run_id': run_id,
                 'status': 'RESERVED', 'max_calls': 1, 'max_output_tokens': 6000,
                 'initial_accounted_usd': str(state['spent'])}
        atomic_json(folder / 'claim.json', claim)
        atomic_json(folder / 'approved-packet.json', packet)
        _workflow_outputs(cached=False, artifact_name=reservation_name(run_id, packet, 'review'))
        return {'request_id': request_id, 'status': 'MANUAL_REVIEW_RESERVED'}


def run_review(settings, ciphertext, request_id, packet_sha256):
    packet = validate_review_packet(settings, ciphertext, request_id, packet_sha256)
    parent = settings.db_path.parent / 'manual-requests' / request_id
    folder = parent / 'review-once'
    with media_claim(settings.db_path.parent, 'manual-requests'):
        existing = _existing(settings, request_id)
        if (parent / 'save-receipt.json').exists():
            raise ValueError('Existing manual save history forbids review re-delivery')
        if not (folder / 'claim.json').exists():
            raise ValueError('A remotely preserved review reservation is required before paid work')
        claim = json.loads((folder / 'claim.json').read_text())
        if claim.get('packet_sha256') != packet_sha256:
            raise ValueError('Changed one-shot review approval packet')
        if claim.get('status') == 'MANUAL_DRAFT_READY':
            return _cached(settings, folder, packet_sha256)
        if claim.get('status') != 'RESERVED' or claim.get('run_id') != _run_identity():
            return {'request_id': request_id, 'status': 'MANUAL_CHECK_REQUIRED'}
        state = _original_state(settings, packet)
        verify_remote_reservation(claim['run_id'], packet, 'review')
        claim['status'] = 'STARTED'
        atomic_json(folder / 'claim.json', claim)
        try:
            post, request = state['post'], state['request']
            if max_title_similarity(post.title, existing) >= settings.config['blog']['max_similarity']:
                raise ValueError('Existing duplicate title blocks this manual review')
            llm = BlogLLM(settings.openai_api_key, settings.openai_model, settings.root,
                          settings.review_model, folder / 'usage.jsonl')
            llm.client = SimpleNamespace(responses=BudgetedResponses(
                llm.client.responses, folder / 'paid-calls.json',
                {**packet['budget'], 'max_calls': 1, 'max_output_tokens': 6000},
                initial_spend=state['spent'], approved_date=packet['date']))
            review = llm.review(post, settings.config['categories']['origins'], request,
                                single_attempt=True)
            score, decision = review_result(review)
            if decision != 'PASS' or score < settings.config['blog']['review_pass_score']:
                raise ValueError('One-shot manual review did not pass; no rewrite or new review allowed')
            if packet['date'] != str(today_kst()):
                raise ValueError('One-shot review completion crossed midnight')
            verify_photos(post.photos)
            post.quality_score, post.status = score, 'MANUAL_DRAFT_READY'
            recovery = {'version': VERSION, 'approval_reference': packet['approval_reference'],
                        'packet_sha256': packet_sha256,
                        'original_claim_sha256': packet['original_claim_sha256'],
                        'original_calls_sha256': packet['original_calls_sha256'],
                        'writer_payload_sha256': packet['writer_payload_sha256'],
                        'writer_response_id': packet['writer_response_id'], 'review_calls': 1}
            result = {'post': post.__dict__, 'input': request.__dict__, 'review': review,
                      'pre_review': {'reused_immutable_rewrite': True, 'model_correction_used': False},
                      'approval': state['claim'], 'review_recovery': recovery}
            digest = _result_digest(result)
            atomic_json(folder / 'result.json', result)
            atomic_json(parent / 'result.json', result)
            atomic_json(parent / 'claim.json', {**state['claim'], 'status': 'MANUAL_DRAFT_READY',
                                                'result_sha256': digest, 'review_recovery': recovery})
            claim.update(status='MANUAL_DRAFT_READY', result_sha256=digest)
            atomic_json(folder / 'claim.json', claim)
            return {'request_id': request_id, 'status': 'MANUAL_DRAFT_READY', 'cached': False}
        except Exception:
            claim['status'] = 'HELD'
            atomic_json(folder / 'claim.json', claim)
            raise
