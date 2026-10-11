"""One owner-approved replacement of a rejected natural parenting manuscript.

This is a separate draft-only result, never an automatic recovery, slot reset or
publication approval. All original files and database rows are read-only.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import threading
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from urllib.parse import urlsplit

from cryptography.fernet import Fernet

from .core import KST, max_title_similarity, recent_titles, review_result, today_kst
from .images import execute_image_plans, image_prompt, section_contexts
from .inputs import ContentRequest, verify_photos
from .llm import BlogLLM
from .manual_costs import usage_cost
from .manual_requests import (
    BudgetedResponses,
    _result_digest,
    _run_identity,
    _workflow_outputs,
    atomic_json,
    check_remote_history,
    reservation_name,
    verify_remote_reservation,
)
from .manual_review import _approval, _digest, _sha
from .planning import active_plan, media_claim
from .pre_review import DraftCandidate, inspect_candidate
from .responses import DRAFT_SCHEMA

VERSION = 'manual-repair-v1'
REQUEST_ID = 'owner-20261011-100day-baby-play'
ORIGINAL_DATE = '2026-10-11'
READY = 'MANUAL_REPAIR_DRAFT_READY'
HELD = 'MANUAL_REPAIR_HELD'
BUDGET = {'review_model': 'gpt-5', 'review_calls': 1, 'review_output_tokens': 6000,
          'max_images': 5, 'max_paid_submissions': 6, 'image_model': 'gpt-image-2.5-flare',
          'image_quality': 'medium', 'image_size': '1024x1024', 'image_format': 'jpeg',
          'image_prompt_max_bytes': 20000, 'max_estimated_usd': '1.50',
          'sdk_retries': 0, 'truncation_retries': 0, 'image_max_attempts': 1}
FIELDS = {'version', 'request_id', 'date', 'approval_reference', 'approved_by', 'scope',
          'category', 'question', 'original_usage_sha256', 'original_request_sha256',
          'original_plan_sha256', 'original_claim_sha256', 'original_checkpoint_sha256',
          'writer_cache_file', 'writer_payload_sha256', 'writer_response_id',
          'original_review_cache_file', 'original_review_sha256', 'corrected_post',
          'corrected_post_sha256', 'evidence', 'budget'}


def _read_pinned(path, digest):
    if not _sha(digest) or path.is_symlink():
        raise ValueError('Invalid original state pin')
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('Original state changed; preserve the rejected natural run')
    return json.loads(raw)


def _cache(root, name):
    if not isinstance(name, str) or not re.fullmatch(r'2026-10-11-[a-f0-9]{64}\.json', name):
        raise ValueError('Exact original response-cache basename required')
    path = root / 'response-cache' / name
    if path.is_symlink():
        raise ValueError('Original cache must not be a symlink')
    return path


def _evidence(packet):
    items = packet['evidence']
    if not isinstance(items, list) or not 1 <= len(items) <= 3:
        raise ValueError('One to three verified official excerpts required')
    seen, evidence = set(), []
    for item in items:
        if (not isinstance(item, dict) or set(item) != {
                'url', 'text', 'sha256', 'retrieved_at', 'verified_by', 'evidence_kind'}
                or not isinstance(item['url'], str) or len(item['url']) > 2000
                or not isinstance(item['text'], str) or not 300 <= len(item['text']) <= 12000
                or item['evidence_kind'] != 'selected_original_passages'
                or item['verified_by'] not in {'operator', 'repository_owner'}
                or hashlib.sha256(item['text'].encode()).hexdigest() != item['sha256']):
            raise ValueError('Verified evidence must contain attributed original text and exact hash')
        url = urlsplit(item['url'])
        if (url.scheme != 'https' or url.hostname not in {
                'www.healthychildren.org', 'www.cdc.gov'}
                or url.username or url.password or url.port or url.fragment
                or any(c.isspace() for c in item['url']) or item['url'] in seen):
            raise ValueError('Distinct official AAP or CDC source URLs required')
        try:
            stamp = datetime.fromisoformat(item['retrieved_at'])
        except (ValueError, TypeError):
            raise ValueError('Actual source retrieval timestamp required') from None
        if (stamp.tzinfo is None or stamp.astimezone(KST).date().isoformat() != packet['date']
                or stamp > datetime.now(KST)):
            raise ValueError('Evidence date must match original approved day')
        seen.add(item['url'])
        evidence.append({**item, 'retrieved_date': packet['date'],
                         'approval_reference': packet['approval_reference']})
    urls = packet['corrected_post'].get('source_urls')
    if not isinstance(urls, list) or len(urls) != len(set(urls)) or set(urls) != seen:
        raise ValueError('Every manuscript source must have its exact verified excerpt')
    return evidence


def _original_state(settings, packet):
    root = settings.db_path.parent
    key = hashlib.sha256(REQUEST_ID.encode()).hexdigest()
    checkpoint = root / 'response-cache' / f'{ORIGINAL_DATE}-pre-review-{key}.json'
    saved = _read_pinned(checkpoint, packet['original_checkpoint_sha256'])
    revision = _read_pinned(checkpoint.with_suffix('.revision.json'), packet['original_claim_sha256'])
    original_request = saved['request']
    plan = original_request.get('provenance', {}).get('daily_plan')
    if (revision.get('stage') != 'pre_review_correction'
            or saved.get('correction_attempted') is not True
            or original_request.get('category') != 'parenting'
            or original_request.get('photos') != []
            or original_request.get('data', {}).get('question') != packet['question']
            or _digest(original_request) != packet['original_request_sha256']
            or _digest(plan) != packet['original_plan_sha256']
            or plan != active_plan(settings) or plan.get('date') != ORIGINAL_DATE):
        raise ValueError('Original corrected request and current plan must remain unchanged')
    raw = (root / 'usage.jsonl').read_bytes()
    if hashlib.sha256(raw).hexdigest() != packet['original_usage_sha256']:
        raise ValueError('Original five-call usage history changed')
    history = [json.loads(line) for line in raw.splitlines() if line.strip()]
    calls = [row for row in history if row.get('request_id') == REQUEST_ID]
    if (len(calls) != 5 or [c.get('stage') for c in calls] != [
            'benchmark', 'writer', 'pre_review_correction', 'reviewer', 'reviewer']
            or [c.get('status') for c in calls] != [
                'completed', 'completed', 'completed', 'incomplete', 'completed']
            or [c.get('attempt') for c in calls] != [1, 1, 1, 1, 2]
            or calls[3].get('incomplete_reason') != 'max_output_tokens'
            or any(c.get('error') for c in calls[:3] + calls[4:])
            or any(c.get('model') not in {'gpt-5', 'gpt-5-2025-08-07'} for c in calls)
            or calls[2].get('response_id') != packet['writer_response_id']):
        raise ValueError('Exactly the original five paid calls are required')
    writer = json.loads(_cache(root, packet['writer_cache_file']).read_text())
    review = _read_pinned(_cache(root, packet['original_review_cache_file']),
                          packet['original_review_sha256'])
    if (writer.get('response', {}).get('id') != packet['writer_response_id']
            or writer['response'].get('status') != 'completed'
            or _digest(writer.get('payload')) != packet['writer_payload_sha256']
            or review.get('response', {}).get('id') != calls[-1]['response_id']
            or review['response'].get('status') != 'completed'
            or review_result(review.get('payload', {})) != (25, 'REWRITE')
            or review['payload'].get('scores', [0])[0] != 3):
        raise ValueError('Original corrected payload and rejected review do not match')
    spent = sum((Decimal(str(c['estimated_text_usd'])) +
                 Decimal(str(c['estimated_search_call_usd'])) for c in calls), Decimal(0))
    if spent != Decimal('0.4253085'):
        raise ValueError('Original natural-call spend requires reconciliation')
    with closing(sqlite3.connect(settings.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Original database requires reconciliation')
        attempts = conn.execute('SELECT * FROM attempts WHERE request_id=?', (REQUEST_ID,)).fetchall()
        if (len(attempts) != 1 or attempts[0]['status'] != 'DROP_REVIEW'
                or attempts[0]['day'] != ORIGINAL_DATE or attempts[0]['category'] != 'parenting'
                or json.loads(attempts[0]['plan_json']) != plan):
            raise ValueError('Only the existing rejected natural attempt is eligible')
        for table in ('posts', 'save_receipts'):
            if conn.execute(f'SELECT 1 FROM {table} WHERE request_id=?', (REQUEST_ID,)).fetchone():
                raise ValueError('Existing post or save receipt forbids replacement')
        titles = recent_titles(conn)
    evidence = _evidence(packet)
    request = ContentRequest(REQUEST_ID, 'parenting', original_request['data'], [],
        {**original_request['provenance'], 'reference_evidence': evidence,
         'manual_repair': {'version': VERSION, 'approval_reference': packet['approval_reference'],
                           'scope': 'draft_only', 'evidence_attribution':
                           'Operator supplied selected original passages; not model-opened full pages'}})
    post, issues, changes = inspect_candidate(
        DraftCandidate(packet['corrected_post'], [e['url'] for e in evidence]), request,
        settings.config['categories']['parenting'],
        settings.config.get('editorial', {}).get('require_structure', True))
    if (post is None or issues or changes or any(
            getattr(post, k) != packet['corrected_post'][k] for k in DRAFT_SCHEMA['required'])):
        raise ValueError('Corrected manuscript must pass deterministic checks without further changes')
    if max_title_similarity(post.title, titles) >= settings.config['blog']['max_similarity']:
        raise ValueError('Duplicate manuscript title')
    return {'post': post, 'request': request, 'spent': spent, 'evidence': evidence}


def validate_repair_packet(settings, ciphertext, request_id, packet_sha256):
    if (request_id != REQUEST_ID or not _sha(packet_sha256)
            or not isinstance(ciphertext, str) or not 0 < len(ciphertext) <= 180000):
        raise ValueError('Invalid repair selector')
    raw = Fernet(os.environ['BLOG_BUNDLE_KEY'].encode()).decrypt(ciphertext.encode())
    if len(raw) > 130000 or hashlib.sha256(raw).hexdigest() != packet_sha256:
        raise ValueError('Repair approval packet digest mismatch')
    packet = json.loads(raw)
    if (not isinstance(packet, dict) or set(packet) != FIELDS or packet['version'] != VERSION
            or packet['request_id'] != request_id or packet['date'] != ORIGINAL_DATE
            or packet['scope'] != 'draft_only' or packet['category'] != 'parenting'
            or packet['approved_by'] != 'repository_owner' or not _approval(packet['approval_reference'])
            or not isinstance(packet['question'], str) or not 1 <= len(packet['question']) <= 500
            or not isinstance(packet['corrected_post'], dict)
            or set(packet['corrected_post']) != set(DRAFT_SCHEMA['required'])
            or packet['corrected_post'].get('as_of_date') != ORIGINAL_DATE
            or _digest(packet['corrected_post']) != packet['corrected_post_sha256']
            or _digest(packet['budget']) != _digest(BUDGET)
            or any(not _sha(packet[k]) for k in FIELDS if k.endswith('_sha256'))):
        raise ValueError('Explicit approval of a fixed parenting replacement and bounded budget required')
    config = settings.config.get('images', {})
    if (settings.openai_model != 'gpt-5' or settings.review_model != 'gpt-5'
            or config.get('model') != BUDGET['image_model']
            or config.get('quality') != 'medium' or config.get('size') != '1024x1024'
            or config.get('output_format') != 'jpeg'
            or type(config.get('parenting_count')) is not int
            or not 1 <= config['parenting_count'] <= 5):
        raise ValueError('Approved review/image configuration drift')
    _original_state(settings, packet)
    return packet


def _folder(settings):
    return settings.db_path.parent / 'manual-repairs' / REQUEST_ID


def _cached(folder, digest):
    claim = json.loads((folder / 'claim.json').read_text())
    if claim.get('packet_sha256') != digest:
        raise ValueError('Repair approval cannot be replaced')
    if claim.get('status') != READY:
        raise ValueError('Repair already claimed; unresolved outcomes cannot be resubmitted')
    result = json.loads((folder / 'result.json').read_text())
    if _result_digest(result) != claim.get('result_sha256'):
        raise ValueError('Completed repair result integrity mismatch')
    verify_photos(result['post']['photos'])
    return {'request_id': REQUEST_ID, 'status': READY, 'cached': True}


def _decode(settings, request_id, packet_sha256):
    return validate_repair_packet(settings, os.environ['BLOG_MANUAL_REPAIR_PACKET'],
                                  request_id, packet_sha256)


def reserve(settings, request_id, packet_sha256):
    packet = _decode(settings, request_id, packet_sha256)
    folder = _folder(settings)
    with media_claim(settings.db_path.parent, 'manual-repairs'):
        if folder.exists():
            result = _cached(folder, packet_sha256)
            _workflow_outputs(cached=True)
            return result
        if packet['date'] != str(today_kst()):
            raise ValueError('Repair approval cannot reset the original date')
        run_id = _run_identity()
        check_remote_history(packet, 'repair')
        folder.mkdir(parents=True, exist_ok=False)
        atomic_json(folder / 'approved-packet.json', packet)
        atomic_json(folder / 'claim.json', {
            'request_id': REQUEST_ID, 'date': ORIGINAL_DATE, 'packet_sha256': packet_sha256,
            'approval_reference': packet['approval_reference'], 'run_id': run_id, 'status': 'RESERVED',
            'scope': 'draft_only', 'budget': BUDGET, 'original_spend_estimated_usd': '0.4253085',
            'additional_scenario_estimated_usd': '1.16585',
            'estimate_basis': 'Review 0.60 + 5 images * 0.11317; assumptions, not billing bounds'})
        _workflow_outputs(cached=False, artifact_name=reservation_name(run_id, packet, 'repair'))
        return {'request_id': REQUEST_ID, 'status': 'MANUAL_REPAIR_RESERVED'}


class PaidLedger:
    """Single review and each independent image claimed durably before submission."""
    def __init__(self, folder):
        self.path = folder / 'paid-calls.json'
        self.lock = threading.Lock()

    def claim(self, stage, params):
        with self.lock:
            if str(today_kst()) != ORIGINAL_DATE:
                raise ValueError('No paid submissions after original approved date')
            calls = json.loads(self.path.read_text()) if self.path.exists() else []
            count = sum(c['stage'] == stage for c in calls)
            if (stage not in {'review', 'image'} or len(calls) >= 6
                    or count >= (1 if stage == 'review' else 5)
                    or (stage == 'image' and not any(c['stage'] == 'review' for c in calls))):
                raise ValueError('Repair paid submission ceiling exhausted')
            if stage == 'image' and (set(params) != {
                    'model', 'prompt', 'quality', 'size', 'output_format', 'n'}
                    or params['model'] != BUDGET['image_model'] or params['quality'] != 'medium'
                    or params['size'] != '1024x1024' or params['output_format'] != 'jpeg'
                    or type(params['n']) is not int or params['n'] != 1
                    or not isinstance(params['prompt'], str) or not params['prompt']
                    or len(params['prompt'].encode()) > 20000):
                raise ValueError('Unapproved image API parameters')
            estimate = Decimal('0.60') if stage == 'review' else Decimal('0.11317')
            if self.accounted(calls) + estimate > Decimal('1.50'):
                raise ValueError('Additional estimated budget exhausted')
            calls.append({'ordinal': len(calls) + 1, 'stage': stage, 'status': 'CLAIMED',
                          'params_sha256': _digest(params),
                          'image_cache_key': hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
                              if stage == 'image' else None,
                          'scenario_estimated_usd': str(estimate)})
            atomic_json(self.path, calls)


    def accounted(self, calls=None):
        calls = json.loads(self.path.read_text()) if calls is None and self.path.exists() else (calls or [])
        total = Decimal(0)
        for call in calls:
            amount = Decimal(call['scenario_estimated_usd'])
            if call['stage'] == 'review':
                path = self.path.parent / 'review-calls.json'
                if path.exists():
                    for row in json.loads(path.read_text()):
                        actual = Decimal(row['accounted_cost_usd'])
                        if not actual.is_finite() or actual < 0:
                            raise ValueError('Invalid review usage accounting')
                        amount = max(amount, actual)
            else:
                manifest = self.path.parent / 'images' / (call['image_cache_key'] + '.json')
                if manifest.exists():
                    actual = usage_cost(json.loads(manifest.read_text()), image=True)
                    if actual is not None:
                        amount = max(amount, actual)
            total += amount
        return total


class RepairResponses:
    def __init__(self, responses, ledger):
        self.responses, self.ledger = responses, ledger

    def create(self, **kwargs):
        if kwargs.get('model') != 'gpt-5' or kwargs.get('max_output_tokens') != 6000:
            raise ValueError('Exactly one 6000-output-token GPT-5 review is authorized')
        self.ledger.claim('review', kwargs)
        return self.responses.create(**kwargs)


def _image_plans(settings, request, post):
    sections = section_contexts(post.body)
    count = min(settings.config['images']['parenting_count'], 1 + len(sections), 5)
    plans, placements = [], []
    for index in range(count):
        section = '글 전체 핵심 요약' if index == 0 else sections[
            round((index - 1) * (len(sections) - 1) / max(1, count - 2))]
        prompt = image_prompt(post, section, thumbnail=index == 0,
                              editorial_type=request.data.get('editorial_type'))
        if len(prompt.encode()) > 20000:
            raise ValueError('Image prompt exceeds approved 20000-byte limit')
        params = {'model': BUDGET['image_model'], 'prompt': prompt, 'quality': 'medium',
                  'size': '1024x1024', 'output_format': 'jpeg', 'n': 1}
        plans.append((params, hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()))
        placements.append({} if index == 0 else {'section_heading': section.splitlines()[0]})
    return plans, placements


def prepare(settings, request_id, packet_sha256):
    packet = _decode(settings, request_id, packet_sha256)
    folder = _folder(settings)
    with media_claim(settings.db_path.parent, 'manual-repairs'):
        path = folder / 'claim.json'
        if not path.exists():
            raise ValueError('Remotely preserved repair reservation required')
        claim = json.loads(path.read_text())
        if claim.get('status') == READY:
            return _cached(folder, packet_sha256)
        if (claim.get('packet_sha256') != packet_sha256 or claim.get('status') != 'RESERVED'
                or claim.get('run_id') != _run_identity() or (folder / 'paid-calls.json').exists()
                or packet['date'] != str(today_kst())):
            raise ValueError('Repair already claimed or changed; no retry allowed')
        state = _original_state(settings, packet)
        plans, placements = _image_plans(settings, state['request'], state['post'])
        verify_remote_reservation(claim['run_id'], packet, 'repair')
        claim['status'] = 'STARTED'
        atomic_json(path, claim)
        try:
            evidence_folder = folder / 'source-evidence'
            evidence_folder.mkdir()
            for evidence in state['evidence']:
                name = 'reference-' + hashlib.sha256(evidence['url'].encode()).hexdigest() + '.json'
                atomic_json(evidence_folder / name, evidence)
            ledger = PaidLedger(folder)
            llm = BlogLLM(settings.openai_api_key, 'gpt-5', settings.root, 'gpt-5', folder / 'usage.jsonl')
            responses = BudgetedResponses(llm.client.responses, folder / 'review-calls.json',
                {'writer_model': 'gpt-5', 'review_model': 'gpt-5', 'max_calls': 1,
                 'max_output_tokens': 6000, 'max_estimated_usd': '1.50'}, approved_date=ORIGINAL_DATE)
            llm.client = SimpleNamespace(responses=RepairResponses(responses, ledger))
            review = llm.review(state['post'], settings.config['categories']['parenting'],
                                state['request'], single_attempt=True)
            atomic_json(folder / 'review.json', review)
            score, decision = review_result(review)
            checked_urls = {c.get('source_url') for c in review.get('source_checks', [])
                            if c.get('status') == 'SUPPORTED' and c.get('evidence', '').strip()}
            if (decision != 'PASS' or score < max(24, settings.config['blog']['review_pass_score'])
                    or not set(state['post'].source_urls) <= checked_urls):
                raise ValueError('One-shot repair review failed; no images or another review allowed')
            post = state['post']

            def before_image(params):
                _original_state(settings, packet)
                ledger.claim('image', params)

            post.photos = execute_image_plans(settings, folder / 'images', plans, placements,
                allow_uncertain_recovery=False, max_attempts=1,
                before_submit=before_image)
            verify_photos(post.photos)
            if packet['date'] != str(today_kst()):
                raise ValueError('Repair completion crossed original approved day')
            post.quality_score, post.status = score, READY
            result = {'post': asdict(post), 'input': asdict(state['request']), 'review': review,
                      'approval': {'version': VERSION, 'packet_sha256': packet_sha256,
                                   'approval_reference': packet['approval_reference'], 'scope': 'draft_only'},
                      'costs': {'original_estimated_usd': str(state['spent']),
                                'additional_scenario_estimated_usd': str(Decimal('0.60') +
                                    Decimal('0.11317') * len(plans)),
                                'combined_scenario_estimated_usd': str(state['spent'] + Decimal('0.60') +
                                    Decimal('0.11317') * len(plans)),
                                'additional_accounted_estimated_usd': str(ledger.accounted()),
                                'combined_accounted_estimated_usd': str(state['spent'] + ledger.accounted()),
                                'budget_is_billing_cap': False},
                      'requires_visual_review': True}
            atomic_json(folder / 'result.json', result)
            claim.update(status=READY, result_sha256=_result_digest(result))
            atomic_json(path, claim)
            return {'request_id': REQUEST_ID, 'status': READY, 'cached': False,
                    'images': len(post.photos), 'requires_visual_review': True}
        except Exception:
            claim['status'] = HELD
            atomic_json(path, claim)
            raise
