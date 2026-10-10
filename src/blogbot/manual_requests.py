"""Owner-authorized extra drafts, isolated from automatic planning and publication.

The operator verifies the owner's approval before encrypting this envelope. Fernet
and a separate digest pin protect transport; an approval string is not evidence
that software authenticated a human conversation. This mode never reads approval
from source pages, generated prose or ordinary input queues.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sqlite3
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

from cryptography.fernet import Fernet
from PIL import Image

from .core import max_title_similarity, recent_titles, review_result, today_kst
from .images import generate_images
from .inputs import ContentRequest, verify_photos
from .llm import BlogLLM
from .manual_costs import estimate_project, thumbnail_spend, usage_cost, validate_budget
from .planning import media_claim
from .pre_review import inspect_candidate, run_pre_review

VERSION = 'manual-request-v1'
SLUG = r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}'


def atomic_json(path: Path, value) -> None:
    """Durable private claim, including directory metadata, before external spend."""
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _text(value, maximum):
    return isinstance(value, str) and bool(value.strip()) and len(value) <= maximum


def validate_packet(settings, ciphertext: str, request_id: str, packet_sha256: str) -> dict:
    if (not isinstance(request_id, str) or not re.fullmatch(SLUG, request_id)
            or not isinstance(packet_sha256, str) or not re.fullmatch(r'[a-f0-9]{64}', packet_sha256)
            or not isinstance(ciphertext, str) or not 0 < len(ciphertext) <= 60_000):
        raise ValueError('Invalid manual request selector')
    raw = Fernet(os.environ['BLOG_BUNDLE_KEY'].encode()).decrypt(ciphertext.encode())
    if len(raw) > 45_000 or hashlib.sha256(raw).hexdigest() != packet_sha256:
        raise ValueError('Manual request packet digest mismatch')
    packet = json.loads(raw)
    expected = {'version', 'request_id', 'date', 'approval_reference', 'approved_by', 'scope',
                'category', 'question', 'context', 'sources', 'thumbnail', 'budget'}
    if (not isinstance(packet, dict) or set(packet) != expected
            or packet['version'] != VERSION or packet['request_id'] != request_id
            or packet['date'] != str(today_kst()) or packet['scope'] != 'draft_only'
            or packet['approved_by'] != 'repository_owner' or packet['category'] != 'origins'
            or not isinstance(packet['approval_reference'], str)
            or not re.fullmatch(r'Sentinel_[A-Za-z0-9_-]{8,100}', packet['approval_reference'])
            or not _text(packet['question'], 500)
            or not isinstance(packet['context'], str) or len(packet['context']) > 5000):
        raise ValueError('Explicit current owner draft approval is required')
    if settings.config['categories']['origins'].get('naver_category_no') != 9:
        raise ValueError('Origins category configuration needs reconciliation')
    sources = packet['sources']
    if not isinstance(sources, list) or not 1 <= len(sources) <= 3:
        raise ValueError('Verified source records required')
    for source in sources:
        if (not isinstance(source, dict) or set(source) != {'url', 'excerpt'}
                or not _text(source['url'], 2000) or not _text(source['excerpt'], 2000)):
            raise ValueError('Invalid source record')
        url = urlsplit(source['url'])
        if (url.scheme != 'https' or not url.hostname or url.username or url.password
                or any(c.isspace() for c in source['url'])):
            raise ValueError('Invalid source URL')
    photo = packet['thumbnail']
    thumbnail_root = settings.db_path.parent / 'manual-thumbnails' / request_id
    if thumbnail_root.exists() and (not isinstance(photo, dict) or 'prepared_request_id' not in photo):
        raise ValueError('Existing thumbnail project requires its exact approved reference')
    if isinstance(photo, dict) and 'prepared_request_id' in photo:
        from .manual_thumbnail import _verify_result
        if (set(photo) != {'prepared_request_id', 'sha256', 'approved', 'role', 'generated', 'caption'}
                or photo['prepared_request_id'] != request_id or photo['approved'] is not True
                or photo['role'] != 'thumbnail' or photo['generated'] is not True
                or not isinstance(photo['caption'], str) or len(photo['caption']) > 500):
            raise ValueError('Approved prepared thumbnail identity mismatch')
        folder = settings.db_path.parent / 'manual-thumbnails' / request_id
        claim = json.loads((folder / 'claim.json').read_text())
        original = json.loads((folder / 'approved-packet.json').read_text())
        if (claim.get('status') != 'MANUAL_THUMBNAIL_READY'
                or original['request_id'] != request_id or original['question'] != packet['question']
                or original['sources'] != packet['sources'] or original['budget'] != packet['budget']):
            raise ValueError('Prepared thumbnail budget/question mismatch')
        result = _verify_result(folder, claim)
        if photo['sha256'] != result['photo']['sha256']:
            raise ValueError('Prepared thumbnail approval hash mismatch')
        data = Path(result['photo']['file']).read_bytes()
        if not 0 < len(data) <= 5_000_000:
            raise ValueError('Prepared thumbnail transport too large')
    else:
        if (not isinstance(photo, dict) or set(photo) != {'data_base64', 'sha256', 'extension',
                                                        'approved', 'role', 'generated', 'caption'}
                or photo['approved'] is not True or photo['role'] != 'thumbnail'
                or type(photo['generated']) is not bool or photo['extension'] not in {'jpg', 'png', 'webp'}
                or not isinstance(photo['caption'], str) or len(photo['caption']) > 500):
            raise ValueError('One approved thumbnail required before paid work')
        data = base64.b64decode(photo['data_base64'], validate=True)
        if not 0 < len(data) <= 30_000 or hashlib.sha256(data).hexdigest() != photo['sha256']:
            raise ValueError('Thumbnail content/hash needs reconciliation')
        signatures = {'jpg': data.startswith(b'\xff\xd8\xff'), 'png': data.startswith(b'\x89PNG\r\n\x1a\n'),
                      'webp': data.startswith(b'RIFF') and data[8:12] == b'WEBP'}
        if not signatures[photo['extension']]:
            raise ValueError('Thumbnail format mismatch')
        with Image.open(io.BytesIO(data)) as image:
            if not (1 <= image.width <= 4096 and 1 <= image.height <= 4096):
                raise ValueError('Thumbnail dimensions exceed approved transport')
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            image.load()
    with Image.open(io.BytesIO(data)) as image:
        if not (1 <= image.width <= 4096 and 1 <= image.height <= 4096):
            raise ValueError('Thumbnail dimensions exceed approved transport')
        image.verify()
    with Image.open(io.BytesIO(data)) as image:
        image.load()
    validate_budget(packet['budget'], settings)
    estimate_project(packet['budget'])
    return packet


class BudgetedResponses:
    """Persist call ceilings BEFORE submission; unknown outcomes never restore them.

    Output reservations and request count are hard ceilings, not a dollar ceiling.
    Input/search tokens and account discounts require billing reconciliation.
    """
    def __init__(self, responses, path: Path, budget: dict, initial_spend=Decimal(0), approved_date=None):
        self.responses, self.path, self.budget = responses, path, budget
        self.initial_spend = initial_spend
        self.approved_date = approved_date

    def create(self, **kwargs):
        if self.approved_date is not None and self.approved_date != str(today_kst()):
            raise ValueError('Dated manual approval crossed midnight; preserve existing results')
        calls = json.loads(self.path.read_text()) if self.path.exists() else []
        tokens = kwargs.get('max_output_tokens')
        if (kwargs.get('model') not in {self.budget['writer_model'], self.budget['review_model']}
                or type(tokens) is not int or tokens <= 0
                or len(calls) >= self.budget['max_calls']
                or sum(c['max_output_tokens'] for c in calls) + tokens > self.budget['max_output_tokens']):
            raise ValueError('Manual paid-call budget exhausted')
        reserved = Decimal('0.53') + Decimal(tokens) * Decimal('0.000010')
        used = self.initial_spend + sum(Decimal(c['accounted_cost_usd']) for c in calls)
        if used + reserved > Decimal(str(self.budget['max_estimated_usd'])):
            raise ValueError('Next-call estimate exceeds remaining approved estimated budget')
        calls.append({'accounted_cost_usd': str(reserved), 'cost_basis': 'preflight_scenario',
                      'ordinal': len(calls) + 1, 'model': kwargs['model'],
                      'max_output_tokens': tokens, 'status': 'STARTED',
                      'actual_call_date_kst': str(today_kst())})
        atomic_json(self.path, calls)
        kwargs['service_tier'] = 'default'
        result = self.responses.create(**kwargs)
        cost = usage_cost(result)
        if cost is not None:
            calls[-1]['accounted_cost_usd'] = str(cost)
            calls[-1]['cost_basis'] = 'reported_usage'
        calls[-1]['status'] = 'RETURNED'
        atomic_json(self.path, calls)
        return result


def _existing(settings, request_id, *, check_unresolved=True):
    # No schema migrations, candidate claims or writes to the automatic database.
    with closing(sqlite3.connect(settings.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        if conn.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
            raise ValueError('Existing database requires reconciliation')
        for table in ('posts', 'attempts', 'save_receipts'):
            if conn.execute(f'SELECT 1 FROM {table} WHERE request_id=?', (request_id,)).fetchone():
                raise ValueError('Existing request identity cannot be reused')
        if check_unresolved and conn.execute("SELECT 1 FROM posts WHERE status IN ('SAVING','SAVE_UNCERTAIN',"
                        "'PUBLISHING','PUBLISH_UNCERTAIN')").fetchone():
            raise ValueError('Unresolved existing write requires reconciliation')
        if check_unresolved and conn.execute("SELECT 1 FROM save_receipts WHERE status IN ('SAVING','SAVE_UNCERTAIN',"
                        "'PUBLISHING','PUBLISH_UNCERTAIN')").fetchone():
            raise ValueError('Unresolved receipt requires reconciliation')
        titles = recent_titles(conn)
    context = settings.db_path.parent / 'context.json'
    if context.exists():
        titles += json.loads(context.read_text()).get('published_titles', [])
    for path in (settings.db_path.parent / 'manual-requests').glob('*/result.json'):
        result = json.loads(path.read_text())
        claim = json.loads(path.with_name('claim.json').read_text())
        if claim.get('status') == 'MANUAL_DRAFT_READY':
            if _result_digest(result) != claim.get('result_sha256'):
                raise ValueError('Historical manual result integrity needs reconciliation')
            if result['post']['request_id'] != request_id:
                titles.append(result['post']['title'])
    return titles


def _result_digest(result: dict) -> str:
    # Local path relocation is allowed; all content/approval/review bytes are pinned.
    canonical = json.loads(json.dumps(result))
    for item in ('post', 'input'):
        for photo in canonical[item]['photos']:
            photo.pop('file', None)
    return hashlib.sha256(json.dumps(canonical, sort_keys=True,
                                     ensure_ascii=False).encode()).hexdigest()


def _run_identity() -> str:
    run_id = os.environ.get('GITHUB_RUN_ID', '')
    if (os.environ.get('GITHUB_ACTIONS') != 'true' or not run_id.isdigit()
            or os.environ.get('GITHUB_RUN_ATTEMPT') != '1'):
        raise ValueError('Manual paid preparation requires a first-attempt owner workflow')
    return run_id


def reservation_name(run_id: str, packet: dict, stage: str = 'manual') -> str:
    parts = [packet['request_id'], packet['approval_reference'],
             re.sub(r'\s+', '', packet['question']).casefold()]
    if stage not in {'manual', 'thumbnail', 'review'}:
        raise ValueError('Invalid reservation stage')
    return f'blog-state-{run_id}-{stage}-' + '-'.join(
        hashlib.sha256(value.encode()).hexdigest() for value in parts)


def check_remote_history(packet: dict, stage: str = 'manual') -> None:
    # An old workflow's pack may omit new files. Artifact names independently
    # retain opaque identity/approval/question claims across that stale lineage.
    from .cloud import github_get
    expected = reservation_name('0', packet, stage).split(f'-{stage}-', 1)[1].split('-')
    for page in range(1, 101):
        listing = json.loads(github_get(f'/actions/artifacts?per_page=100&page={page}'))
        artifacts = listing['artifacts']
        if not isinstance(artifacts, list) or any(not isinstance(a, dict) for a in artifacts):
            raise ValueError('Malformed remote manual reservation history')
        for artifact in artifacts:
            match = re.fullmatch(r'blog-state-\d+-' + stage + r'-([a-f0-9]{64})-'
                                 r'([a-f0-9]{64})-([a-f0-9]{64})', artifact['name'])
            if match and any(a == b for a, b in zip(match.groups(), expected, strict=True)):
                raise ValueError('Remote manual reservation already consumes this request or approval')
        if len(artifacts) < 100:
            return
    raise ValueError('Remote manual reservation history is incomplete')


def _workflow_outputs(*, cached: bool, artifact_name: str = '') -> None:
    path = os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path, 'a', encoding='utf-8') as stream:
            stream.write(f'cached={str(cached).lower()}\nartifact_name={artifact_name}\n')


def approved_source_evidence(packet: dict) -> list[dict]:
    """Attribute owner-approved excerpts as supplied evidence, never as model browsing.

    This runs only for a fresh authenticated packet, not retroactively on cached
    reviewer contexts. The actual excerpt is included in that reviewer's prompt.
    """
    return [{'url': source['url'], 'text': source['excerpt'],
             'sha256': hashlib.sha256(source['excerpt'].encode()).hexdigest(),
             'verified_by': 'operator_verified_owner_approved_packet',
             'approval_reference': packet['approval_reference'],
             'supplied_date': packet['date']}
            for source in packet['sources']]


def reserve_manual_request(settings, ciphertext: str, request_id: str, packet_sha256: str) -> dict:
    packet = validate_packet(settings, ciphertext, request_id, packet_sha256)
    directory = settings.db_path.parent / 'manual-requests' / request_id
    with media_claim(settings.db_path.parent, 'manual-requests'):
        _existing(settings, request_id)
        if directory.exists():
            if (directory / 'save-receipt.json').exists():
                raise ValueError('Existing manual save history forbids a new draft delivery')
            claim = json.loads((directory / 'claim.json').read_text())
            if claim.get('packet_sha256') == packet_sha256 and claim.get('status') == 'MANUAL_DRAFT_READY':
                result = json.loads((directory / 'result.json').read_text())
                if _result_digest(result) != claim.get('result_sha256'):
                    raise ValueError('Manual result integrity needs reconciliation')
                verify_photos(result['post']['photos'])
                _workflow_outputs(cached=True)
                return {'request_id': request_id, 'status': 'MANUAL_DRAFT_READY', 'cached': True}
            raise ValueError('Manual request already reserved; do not submit again')
        run_id = _run_identity()
        if 'prepared_request_id' not in packet['thumbnail']:
            check_remote_history(packet, 'thumbnail')
        check_remote_history(packet)
        question_sha256 = hashlib.sha256(
            re.sub(r'\s+', '', packet['question']).casefold().encode()).hexdigest()
        for other in directory.parent.glob('*/claim.json'):
            previous = json.loads(other.read_text())
            if (previous.get('approval_reference') == packet['approval_reference']
                    or previous.get('question_sha256') == question_sha256):
                raise ValueError('Existing approval or question cannot be retried under a new ID')
        directory.mkdir(parents=True, exist_ok=False)
        claim = {'request_id': request_id, 'date': packet['date'], 'packet_sha256': packet_sha256,
                 'approval_reference': packet['approval_reference'], 'scope': 'draft_only',
                 'status': 'RESERVED', 'budget': packet['budget'], 'run_id': run_id,
                 'question_sha256': question_sha256,
                 'cost_estimate': estimate_project(packet['budget'])}
        atomic_json(directory / 'claim.json', claim)
        atomic_json(directory / 'approved-packet.json', packet)
    _workflow_outputs(cached=False, artifact_name=reservation_name(run_id, packet))
    return {'request_id': request_id, 'status': 'MANUAL_REQUEST_RESERVED'}


def verify_remote_reservation(run_id: str, packet: dict, stage: str = 'manual') -> None:
    # The mandatory upload step must finish before a paid call. A fresh later
    # runner sees RESERVED from this artifact and cannot consume another run's slot.
    from .cloud import github_get
    listing = json.loads(github_get(f'/actions/runs/{run_id}/artifacts?per_page=100'))
    name = reservation_name(run_id, packet, stage)
    artifacts = listing['artifacts']
    if not isinstance(artifacts, list) or any(not isinstance(a, dict) for a in artifacts):
        raise ValueError('Malformed remote reservation verification')
    if not any(a.get('name') == name and not a.get('expired', True)
               for a in artifacts):
        raise ValueError('Remote manual reservation upload is not verified')


def run_manual_request(settings, ciphertext: str, request_id: str, packet_sha256: str) -> dict:
    packet = validate_packet(settings, ciphertext, request_id, packet_sha256)
    directory = settings.db_path.parent / 'manual-requests' / request_id
    with media_claim(settings.db_path.parent, 'manual-requests'):
        existing = _existing(settings, request_id)
        if (directory / 'save-receipt.json').exists():
            raise ValueError('Existing manual save history forbids re-delivery as a new draft')
        claim_path = directory / 'claim.json'
        if not claim_path.exists():
            raise ValueError('A remotely preserved reservation is required before paid work')
        claim = json.loads(claim_path.read_text())
        if claim.get('packet_sha256') != packet_sha256:
            raise ValueError('Changed manual approval packet; preserve original claim')
        if claim.get('status') == 'MANUAL_DRAFT_READY':
            result = json.loads((directory / 'result.json').read_text())
            if _result_digest(result) != claim.get('result_sha256'):
                raise ValueError('Manual result integrity needs reconciliation')
            verify_photos(result['post']['photos'])
            return {'request_id': request_id, 'status': 'MANUAL_DRAFT_READY', 'cached': True}
        if claim.get('status') != 'RESERVED' or claim.get('run_id') != _run_identity():
            return {'request_id': request_id, 'status': 'MANUAL_CHECK_REQUIRED'}
        verify_remote_reservation(claim['run_id'], packet)
        claim['status'] = 'STARTED'
        atomic_json(claim_path, claim)
        try:
            thumbnail = packet['thumbnail']
            if 'prepared_request_id' in thumbnail:
                prepared = json.loads((settings.db_path.parent / 'manual-thumbnails' / request_id /
                                       'result.json').read_text())
                data = Path(prepared['photo']['file']).read_bytes()
                extension = 'jpg'
            else:
                data = base64.b64decode(thumbnail['data_base64'], validate=True)
                extension = thumbnail['extension']
            photo_path = directory / ('thumbnail.' + extension)
            photo_path.write_bytes(data)
            photo = {k: thumbnail[k] for k in ('sha256', 'approved', 'role', 'generated', 'caption')}
            photo['file'] = str(photo_path.resolve())
            request = ContentRequest(request_id, 'origins',
                                     {'question': packet['question'], 'context': packet['context'],
                                      'sources': packet['sources']}, [photo],
                                     {'manual_request': {'version': VERSION, 'date': packet['date'],
                                                         'scope': 'draft_only'},
                                      'reference_evidence': approved_source_evidence(packet)})
            verify_photos(request.photos)
            info = settings.config['categories']['origins']
            llm = BlogLLM(settings.openai_api_key, settings.openai_model, settings.root,
                          settings.review_model, settings.db_path.parent / 'usage.jsonl')
            llm.client = SimpleNamespace(responses=BudgetedResponses(
                llm.client.responses, directory / 'paid-calls.json', packet['budget'],
                initial_spend=thumbnail_spend(settings, request_id), approved_date=packet['date']))
            candidate = llm.create_draft(request, info, existing, raw=True)
            post, pre_review = run_pre_review(settings.db_path.parent, llm, candidate, request, info,
                                             settings.config.get('editorial', {}).get('require_structure', True))
            if max_title_similarity(post.title, existing) >= settings.config['blog']['max_similarity']:
                raise ValueError('Duplicate manual draft title')
            review = llm.review(post, info, request)
            score, decision = review_result(review)
            if (decision == 'REWRITE' and not pre_review['model_correction_used']
                    and score >= settings.config['blog']['rewrite_score']):
                candidate = llm.rewrite(post, info, review, request, single_attempt=True, raw=True)
                post, issues, _ = inspect_candidate(candidate, request, info,
                                                    settings.config.get('editorial', {}).get('require_structure', True))
                if issues:
                    raise ValueError('Manual revision failed deterministic checks')
                review = llm.review(post, info, request)
                score, decision = review_result(review)
            if (decision != 'PASS' or score < settings.config['blog']['review_pass_score']
                    or max_title_similarity(post.title, existing) >= settings.config['blog']['max_similarity']):
                raise ValueError('Manual draft failed review or duplicate checks')
            if packet['date'] != str(today_kst()):
                raise ValueError('Manual completion crossed midnight; retain paid results for review')
            post = generate_images(settings, request, post, allow_uncertain_recovery=False)
            post.quality_score, post.status = score, 'MANUAL_DRAFT_READY'
            result = {'post': asdict(post), 'input': asdict(request), 'review': review,
                      'pre_review': pre_review, 'approval': dict(claim)}
            atomic_json(directory / 'result.json', result)
            claim['result_sha256'] = _result_digest(result)
            claim['status'] = 'MANUAL_DRAFT_READY'
            atomic_json(claim_path, claim)
            return {'request_id': request_id, 'status': 'MANUAL_DRAFT_READY', 'cached': False}
        except Exception:
            claim['status'] = 'HELD'
            atomic_json(claim_path, claim)
            raise


def reconcile_additional_receipts(settings, payload: dict) -> None:
    """Preserve explicit extra-save evidence without consuming the ordinary quota.

    Ordinary records are never reclassified here. Only a retained, separately
    approved manual result can accept this optional additional_records channel.
    """
    from .core import validate_work_receipt
    if payload.get('verified_date') != str(today_kst()):
        raise ValueError('Additional receipts require a current verified ledger')
    extras = payload.get('additional_records', [])
    if not isinstance(extras, list):
        raise TypeError('Malformed additional save receipts')
    ordinary = {row.get('request_id') for row in payload.get('records', [])}
    for request_id in ordinary:
        if (isinstance(request_id, str) and re.fullmatch(SLUG, request_id)
                and (settings.db_path.parent / 'manual-requests' / request_id / 'claim.json').exists()):
            raise ValueError('Explicit extra request cannot enter ordinary quota receipts')
    for record in extras:
        request_id = record.get('request_id', '')
        if (not re.fullmatch(SLUG, request_id) or request_id in ordinary
                or record.get('category') != 'origins'
                or record.get('status') not in {'SAVING', 'SAVE_UNCERTAIN', 'SAVED_NAVER', 'SAVE_NOT_SAVED'}):
            raise ValueError('Invalid or reclassified additional draft receipt')
        folder = settings.db_path.parent / 'manual-requests' / request_id
        claim = json.loads((folder / 'claim.json').read_text())
        result = json.loads((folder / 'result.json').read_text())
        if (claim.get('status') != 'MANUAL_DRAFT_READY'
                or record.get('approval_reference') != claim['approval_reference']
                or record.get('result_sha256') != claim['result_sha256']
                or _result_digest(result) != claim['result_sha256']):
            raise ValueError('Additional save is not bound to its approved manual result')
        checked = validate_work_receipt(record, settings.naver_blog_id)
        _existing(settings, request_id, check_unresolved=False)  # Identity collision, not old-state gating.
        target = folder / 'save-receipt.json'
        if target.exists():
            previous = json.loads(target.read_text())
            if (previous['day'] != checked['day']
                    or (previous['status'] in {'SAVED_NAVER', 'SAVE_NOT_SAVED'}
                        and checked['status'] != previous['status'])):
                raise ValueError('Additional receipt date/status cannot be reset')
        atomic_json(target, checked)
    for path in (settings.db_path.parent / 'manual-requests').glob('*/save-receipt.json'):
        if json.loads(path.read_text())['status'] in {'SAVING', 'SAVE_UNCERTAIN'}:
            raise ValueError('Uncertain additional save requires reconciliation before another write')
