"""Metadata-only manual Work requests; no generation, browser clicks or publication.

The operator verifies the actual owner instruction and browser ledger first.
This local journal is not a distributed browser lock or a paid-call approval.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import closing
from datetime import date, datetime
from pathlib import Path

from .core import KST, today_kst
from .planning import media_claim
from .storage import atomic_json

VERSION = 'work-operation-v1'
SLUG = r'[A-Za-z0-9][A-Za-z0-9_-]{0,79}'
CATEGORIES = {'parenting', 'exercise', 'investment', 'origins', 'cooking'}
STATUSES = {'REGISTERED', 'STARTED', 'COMPLETED', 'HELD', 'SAVED_NAVER',
            'SAVE_UNCERTAIN', 'SAVE_NOT_SAVED'}
MAX_BYTES = 20_000


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _slug(value):
    return isinstance(value, str) and re.fullmatch(SLUG, value) is not None


def _sha(value):
    return isinstance(value, str) and re.fullmatch(r'[a-f0-9]{64}', value) is not None


def _stamp(value):
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp > datetime.now(KST):
        raise ValueError('Work evidence requires an observed time with timezone')
    return stamp


def validate_manifest(manifest, *, current=False):
    fields = {'version', 'operation_id', 'request_id', 'category', 'action',
              'approval_reference', 'approved_by', 'approved_date', 'scope', 'target_sha256'}
    if (not isinstance(manifest, dict) or set(manifest) != fields
            or manifest['version'] != VERSION or not _slug(manifest['operation_id'])
            or not _slug(manifest['request_id']) or manifest['category'] not in CATEGORIES
            or manifest['action'] not in {'create', 'revise', 'save'}
            or manifest['scope'] != 'draft_only' or manifest['approved_by'] != 'repository_owner'
            or not isinstance(manifest['approval_reference'], str)
            or not re.fullmatch(r'Sentinel_[A-Za-z0-9_-]{8,100}', manifest['approval_reference'])
            or (manifest['action'] == 'create' and manifest['target_sha256'] != '')
            or (manifest['action'] != 'create' and not _sha(manifest['target_sha256']))):
        raise ValueError('Invalid explicit manual Work approval')
    approved = date.fromisoformat(manifest['approved_date'])
    if approved > today_kst() or (current and approved != today_kst()):
        raise ValueError('Manual Work approval is not valid for a new action today')
    return manifest


def validate_record(record):
    if (not isinstance(record, dict) or set(record) != {
            'manifest', 'manifest_sha256', 'status', 'registered_at', 'started_at', 'result'}):
        raise ValueError('Malformed manual Work journal')
    manifest = validate_manifest(record['manifest'])
    if record['manifest_sha256'] != digest(manifest) or record['status'] not in STATUSES:
        raise ValueError('Manual Work journal integrity mismatch')
    registered = _stamp(record['registered_at'])
    if registered.astimezone(KST).date().isoformat() != manifest['approved_date']:
        raise ValueError('Manual Work registration must preserve its approval day')
    started = _stamp(record['started_at']) if record['started_at'] else None
    if started and (started < registered or started.astimezone(KST).date().isoformat()
                    != manifest['approved_date']):
        raise ValueError('Manual Work start must preserve its original approval')
    status, result = record['status'], record['result']
    if status in {'REGISTERED', 'STARTED'}:
        if result is not None or bool(started) != (status == 'STARTED'):
            raise ValueError('Manual Work start/result state mismatch')
        return record
    if started is None or not isinstance(result, dict):
        raise ValueError('Manual Work result requires an existing start')
    fields = {'outcome', 'content_sha256', 'evidence_ref', 'checked_at'}
    if status == 'SAVED_NAVER':
        fields.add('saved_at')
    elif status == 'SAVE_NOT_SAVED':
        fields.add('save_check')
    if (set(result) != fields or result['outcome'] != status
            or not isinstance(result['evidence_ref'], str)
            or not 1 <= len(result['evidence_ref']) <= 500
            or _stamp(result['checked_at']) < started
            or (result['content_sha256'] != '' and not _sha(result['content_sha256']))
            or (status != 'HELD' and not _sha(result['content_sha256']))):
        raise ValueError('Manual Work completion needs exact observed evidence')
    if manifest['action'] == 'save':
        if status == 'COMPLETED' or (status != 'HELD'
                and result['content_sha256'] != manifest['target_sha256']):
            raise ValueError('Manual save must match its approved content')
        if status == 'SAVED_NAVER' and not (
                started <= _stamp(result['saved_at']) <= _stamp(result['checked_at'])):
            raise ValueError('Manual save time must be observed after its start')
        if status == 'SAVE_NOT_SAVED':
            from .core import validate_work_receipt
            check = result['save_check']
            if (_stamp(check['attempted_at']) < started
                    or check['checked_at'] != result['checked_at']):
                raise ValueError('Manual absence check must follow the original operation')
            validate_work_receipt({'request_id': manifest['request_id'],
                                   'status': status, 'day': _stamp(check['attempted_at']).astimezone(KST).date().isoformat(),
                                   'save_check': check})
    elif status not in {'COMPLETED', 'HELD'}:
        raise ValueError('Draft/revision completion cannot claim a save')
    return record


def records(directory: Path):
    folder = directory / 'work-operations'
    if folder.is_symlink():
        raise ValueError('Manual Work journal cannot be a symlink')
    output = []
    for path in sorted(folder.glob('*')):
        if (path.is_symlink() or not path.is_file() or path.suffix != '.json'
                or not _slug(path.stem) or not 0 < path.stat().st_size <= MAX_BYTES):
            raise ValueError('Invalid managed manual Work journal file')
        record = validate_record(json.loads(path.read_text(encoding='utf-8')))
        if record['manifest']['operation_id'] != path.stem:
            raise ValueError('Manual Work filename does not match its operation')
        output.append((path, record))
    return output


def exclusions(settings):
    return {record['manifest']['request_id'] for _, record in records(settings.db_path.parent)}


def pending_writes(settings):
    return [record['manifest']['operation_id'] for _, record in records(settings.db_path.parent)
            if record['status'] == 'SAVE_UNCERTAIN'
            or (record['status'] in {'STARTED', 'HELD'}
                and record['manifest']['action'] in {'revise', 'save'})]


def register(settings, manifest):
    manifest = validate_manifest(manifest, current=True)
    if manifest['category'] not in settings.config['categories']:
        raise ValueError('Manual Work category is not configured')
    directory = settings.db_path.parent
    with media_claim(directory, 'work-operations'):
        existing = records(directory)
        for _, record in existing:
            if record['manifest']['operation_id'] == manifest['operation_id']:
                if record['manifest'] != manifest:
                    raise ValueError('Existing manual Work approval cannot be replaced')
                return {'operation_id': manifest['operation_id'], 'status': record['status'], 'cached': True}
        if manifest['action'] == 'create':
            if any(record['manifest']['request_id'] == manifest['request_id'] for _, record in existing):
                raise ValueError('Existing manual request cannot be created again')
            with closing(sqlite3.connect(settings.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
                for table in ('posts', 'attempts', 'save_receipts'):
                    if conn.execute(f'SELECT 1 FROM {table} WHERE request_id=?',
                                    (manifest['request_id'],)).fetchone():
                        raise ValueError('Existing automatic request cannot become a new manual draft')
            if (directory / 'manual-requests' / manifest['request_id']).exists():
                raise ValueError('Existing paid manual request cannot be created again')
        record = {'manifest': manifest, 'manifest_sha256': digest(manifest),
                  'status': 'REGISTERED', 'registered_at': datetime.now(KST).isoformat(),
                  'started_at': '', 'result': None}
        validate_record(record)
        folder = directory / 'work-operations'
        folder.mkdir(exist_ok=True)
        atomic_json(folder / (manifest['operation_id'] + '.json'), record)
    return {'operation_id': manifest['operation_id'], 'status': 'REGISTERED', 'cached': False}


def start(settings, operation_id, target_sha256=''):
    with media_claim(settings.db_path.parent, 'work-operations'):
        entries = records(settings.db_path.parent)
        matches = [(p, r) for p, r in entries if r['manifest']['operation_id'] == operation_id]
        if len(matches) != 1:
            raise ValueError('An existing approved manual Work operation is required')
        path, record = matches[0]
        manifest = validate_manifest(record['manifest'], current=True)
        if record['status'] != 'REGISTERED' or target_sha256 != manifest['target_sha256']:
            raise ValueError('Manual Work operation already started or target content changed')
        if any(r['status'] == 'STARTED' for _, r in entries) or pending_writes(settings):
            raise ValueError('Reconcile the existing manual operation before starting another')
        with closing(sqlite3.connect(settings.db_path.resolve().as_uri() + '?mode=ro', uri=True)) as conn:
            for table in ('posts', 'save_receipts'):
                if conn.execute(f"SELECT 1 FROM {table} WHERE status IN "
                                "('SAVING','SAVE_UNCERTAIN','PUBLISHING','PUBLISH_UNCERTAIN')").fetchone():
                    raise ValueError('Reconcile the automatic write before manual Work')
        record.update(status='STARTED', started_at=datetime.now(KST).isoformat())
        validate_record(record)
        atomic_json(path, record)
    return {'operation_id': operation_id, 'status': 'STARTED', 'scope': 'draft_only'}


def finish(settings, operation_id, result):
    if not isinstance(result, dict):
        raise TypeError('Manual Work completion requires an observed result object')
    with media_claim(settings.db_path.parent, 'work-operations'):
        matches = [(p, r) for p, r in records(settings.db_path.parent)
                   if r['manifest']['operation_id'] == operation_id]
        if len(matches) != 1:
            raise ValueError('Unknown manual Work operation')
        path, record = matches[0]
        if record['result'] == result:
            return {'operation_id': operation_id, 'status': record['status'], 'cached': True}
        reconciliation = (record['status'] in {'SAVE_UNCERTAIN', 'HELD'}
                          and record['manifest']['action'] == 'save'
                          and result.get('outcome') in {'SAVED_NAVER', 'SAVE_NOT_SAVED'}
                          and _stamp(result['checked_at']) >= _stamp(record['result']['checked_at']))
        reconciliation |= (record['status'] == 'HELD'
                           and record['manifest']['action'] == 'revise'
                           and result.get('outcome') == 'COMPLETED'
                           and _stamp(result['checked_at']) >= _stamp(record['result']['checked_at']))
        if record['status'] != 'STARTED' and not reconciliation:
            raise ValueError('Manual Work completion cannot be reset or retried')
        updated = {**record, 'status': result['outcome'], 'result': result}
        validate_record(updated)
        if updated['status'] == 'SAVE_NOT_SAVED':
            from .core import validate_work_receipt
            check = result['save_check']
            owner = (settings.naver_blog_id or settings.config.get('creator_advisor', {}).get('channel_id', ''))
            validate_work_receipt({'request_id': record['manifest']['request_id'],
                                   'status': 'SAVE_NOT_SAVED',
                                   'day': _stamp(check['attempted_at']).astimezone(KST).date().isoformat(),
                                   'save_check': check}, owner)
        atomic_json(path, updated)
    return {'operation_id': operation_id, 'status': updated['status'], 'cached': False}


def merge_record(local, incoming):
    """Choose monotonically advanced evidence; never replay an older action."""
    validate_record(local)
    validate_record(incoming)
    if (local['manifest'] != incoming['manifest'] or local['registered_at'] != incoming['registered_at']
            or (local['started_at'] and incoming['started_at']
                and local['started_at'] != incoming['started_at'])):
        raise ValueError('Conflicting manual Work identity or execution history')
    if local == incoming:
        return local
    rank = {'REGISTERED': 0, 'STARTED': 1, 'SAVE_UNCERTAIN': 2, 'HELD': 2,
            'COMPLETED': 3, 'SAVED_NAVER': 3, 'SAVE_NOT_SAVED': 3}
    if rank[local['status']] == rank[incoming['status']]:
        raise ValueError('Conflicting manual Work outcomes require reconciliation')
    older, newer = sorted((local, incoming), key=lambda r: rank[r['status']])
    if older['status'] in {'SAVE_UNCERTAIN', 'HELD'}:
        allowed = ({'SAVED_NAVER', 'SAVE_NOT_SAVED'} if older['manifest']['action'] == 'save'
                   else {'COMPLETED'} if older['manifest']['action'] == 'revise' else set())
        if (newer['status'] not in allowed
                or (older['manifest']['action'] == 'save' and older['result']['content_sha256']
                    and newer['result']['content_sha256'] != older['result']['content_sha256'])
                or _stamp(newer['result']['checked_at']) < _stamp(older['result']['checked_at'])):
            raise ValueError('Uncertain manual Work needs later observation without another submission')
    return newer
