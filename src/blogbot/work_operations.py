"""Metadata-only Work requests and externally versioned exchange lifecycle guards.

The operator verifies the actual owner instruction and browser ledger first.
The manual local journal is not a distributed browser lock or a paid-call approval.
Exchange coordination requires injected authoritative external read/CAS operations.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import closing, contextmanager
from copy import deepcopy
from datetime import date, datetime, timedelta
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


def exchange_slot(config, scheduled_at, now):
    """Use the scheduler's original occurrence, never the delayed invocation hour."""
    slot, now = (datetime.fromisoformat(value) if isinstance(value, str) else value
                 for value in (scheduled_at, now))
    if slot.tzinfo is None or now.tzinfo is None:
        raise ValueError('Exchange times require timezone offsets')
    slot, now = slot.astimezone(KST), now.astimezone(KST)
    policy = config['operations']['exchange']
    if (slot.hour not in {22, 23, *range(9)} or slot.hour not in policy['hours']
            or slot.minute != policy['minute']
            or slot.second or slot.microsecond
            or slot < datetime.fromisoformat(policy['effective_start'])):
        raise ValueError('An original scheduled night occurrence is required')
    night = (slot - timedelta(days=1)).date() if slot.hour <= 8 else slot.date()
    finish = min(slot + timedelta(hours=1),
                 datetime.combine(night + timedelta(days=1), datetime.min.time(), KST)
                 + timedelta(hours=9))
    cutoff = min(slot + timedelta(minutes=policy['work_minutes']),
                 finish - timedelta(minutes=policy['closing_minutes']))
    return {'slot_id': 'night-exchange-' + slot.strftime('%Y%m%dT%H%M%S') + '-KST',
            'night_id': night.isoformat() + '-night', 'scheduled_at': slot.isoformat(),
            'work_cutoff': cutoff.isoformat(), 'finish_deadline': finish.isoformat(),
            'phase': ('EARLY' if now < slot else 'WORK' if now < cutoff
                      else 'CLOSING' if now < finish else 'EXPIRED')}


def _exchange_update(read, compare_and_swap, edit):
    revision, current = read()
    if revision is None or not isinstance(current, dict) or 'browser_claim' not in current:
        raise ValueError('Authoritative shared ledger revision and claim inventory are required')
    updated = deepcopy(current)
    result = edit(updated)
    if compare_and_swap(revision, updated) is not True:
        raise RuntimeError('Exchange ledger version conflict; no action permitted')
    return result


def _exchange_receipt(action, proof, now):
    if (action['status'] not in {'UNCERTAIN', 'VERIFIED'} or not isinstance(proof, dict)
            or proof.get('kind') != action['kind'] or proof.get('target_id') != action['target_id']
            or proof.get('registered') is not True or proof.get('owned') is not True
            or not isinstance(proof.get('native_id'), str) or not proof['native_id'].strip()
            or not isinstance(proof.get('evidence_ref'), str) or not proof['evidence_ref'].strip()):
        raise ValueError('Only observed native registration evidence counts as performance')
    checked = datetime.fromisoformat(proof['checked_at'])
    registered = datetime.fromisoformat(proof['registered_at'])
    attempted = datetime.fromisoformat(action['attempted_at'])
    if (checked.tzinfo is None or registered.tzinfo is None
            or not attempted <= registered <= checked <= now):
        raise ValueError('Registration evidence must follow the original intent')


def reconcile_exchange_action(read, compare_and_swap, action_id, proof, now):
    """Read-only native reconciliation after a failed result write; never submit again."""
    def edit(state):
        action = state['exchange_actions'][action_id]
        _exchange_receipt(action, proof, now)
        if action['status'] == 'VERIFIED' and action['proof'] != proof:
            raise ValueError('Existing native registration evidence cannot be replaced')
        action.update(status='VERIFIED', proof=deepcopy(proof))
    _exchange_update(read, compare_and_swap, edit)


class ExchangeRun:
    """Metadata guard for an already-authorized external Work executor.

    read/CAS must cover the SAME shared external ledger as writing/manual runs.
    This does not connect a browser, grant permission or provide a local-lock fallback.
    """

    def __init__(self, config, scheduled_at, execution_id, read, compare_and_swap,
                 execution_status, clock):
        if not _slug(execution_id):
            raise ValueError('A unique actual execution ID is required')
        self.config, self.scheduled_at, self.execution_id = config, scheduled_at, execution_id
        self.read, self.compare_and_swap = read, compare_and_swap
        self.execution_status, self.clock = execution_status, clock
        self.slot = exchange_slot(config, scheduled_at, clock())
        self.token = uuid.uuid4().hex
        self.acquired = False

    def acquire(self, targets):
        """One caller wins a slot; TTL alone never releases a living or unknown run."""
        now = self.clock()
        self.slot = exchange_slot(self.config, self.scheduled_at, now)
        if (set(targets) != {'comments', 'likes', 'outgoing_requests'}
                or any(type(n) is not int or n < 0 for n in targets.values())
                or targets['comments'] != 10 or targets['likes'] != 10
                or targets['outgoing_requests'] > 20):
            raise ValueError('Keep the approved pre-action targets')

        def edit(state):
            runs = state.setdefault('exchange_runs', {})
            previous = runs.get(self.slot['slot_id'])
            if previous and (previous.get('invocation_token') != self.token
                             or previous['status'] != 'WAITING'):
                return False  # No slot replay, including failed/expired/uncertain runs.
            record = previous or {**self.slot, 'execution_id': self.execution_id,
                                  'invocation_token': self.token, 'invoked_at': now.isoformat(),
                                  'targets': dict(targets)}
            runs[self.slot['slot_id']] = record
            if record['targets'] != targets:
                raise ValueError('A slot target cannot be changed retrospectively')
            if self.slot['phase'] != 'WORK':
                record.update(status=('WAITING' if self.slot['phase'] == 'EARLY'
                                      else 'NOT_EXECUTED'), reason=self.slot['phase'])
                return False
            claim = state.get('browser_claim')
            if claim:
                expiry = datetime.fromisoformat(claim['expires_at'])
                if expiry.tzinfo is None:
                    raise ValueError('Unknown browser lease expiry; preserve the claim')
                terminal = None
                if expiry <= now:
                    try:
                        terminal = self.execution_status(claim['execution_id'])
                    except (OSError, RuntimeError):
                        terminal = None
                if not terminal or terminal.get('execution_id') != claim['execution_id']:
                    record.update(status='WAITING', reason='BROWSER_BUSY_OR_UNCONFIRMED')
                    return False
                checked = datetime.fromisoformat(terminal['checked_at'])
                if (terminal.get('status') not in {'completed', 'failed', 'cancelled'}
                        or terminal.get('browser_work_ended') is not True
                        or checked.tzinfo is None or not expiry <= checked <= now
                        or now - checked > timedelta(minutes=5) or not terminal.get('evidence_ref')):
                    record.update(status='WAITING', reason='BROWSER_BUSY_OR_UNCONFIRMED')
                    return False
                state.setdefault('browser_releases', []).append(
                    {**claim, 'released_at': now.isoformat(), 'reason': 'EXPIRED_AND_TERMINAL',
                     'terminal_evidence': deepcopy(terminal)})
                state['browser_claim'] = None
                old = next((r for r in runs.values()
                            if r['execution_id'] == claim['execution_id']), None)
                if old:
                    old.update(status='UNCONFIRMED', reason='ABNORMAL_TERMINATION',
                               reconciled_at=checked.isoformat())
            if exchange_slot(self.config, self.scheduled_at, self.clock())['phase'] != 'WORK':
                record.update(status='NOT_EXECUTED', reason='CUTOFF_DURING_ACQUISITION')
                return False
            state['browser_claim'] = {'execution_id': self.execution_id,
                                      'invocation_token': self.token,
                                      'slot_id': self.slot['slot_id'],
                                      'expires_at': self.slot['finish_deadline']}
            record.update(status='RUNNING', started_at=now.isoformat(), reason='')
            return True
        self.acquired = _exchange_update(self.read, self.compare_and_swap, edit)
        if (self.acquired
                and exchange_slot(self.config, self.scheduled_at, self.clock())['phase'] != 'WORK'):
            def expired(state):
                state['exchange_runs'][self.slot['slot_id']].update(
                    status='NOT_EXECUTED', reason='CUTOFF_DURING_ACQUISITION')
            try:
                _exchange_update(self.read, self.compare_and_swap, expired)
            finally:
                self.close()
        return self.acquired

    def perform(self, kind, target_id, submit):
        """Persist uncertainty BEFORE an authorized submission; never retry a target.

        submit receives the absolute cutoff and must bound every browser call by it.
        Permission/access checks remain the executor's existing responsibility.
        """
        if kind not in {'comments', 'likes', 'outgoing_requests', 'replies'} or not target_id:
            raise ValueError('An exact native action target is required')
        now = self.clock()
        if (not self.acquired or exchange_slot(self.config, self.scheduled_at, now)['phase'] != 'WORK'):
            raise ValueError('No new submission outside the original work window')
        action_id = digest({'kind': kind, 'target_id': target_id})

        def edit(state):
            claim = state.get('browser_claim')
            if (not claim or claim['execution_id'] != self.execution_id
                    or claim.get('invocation_token') != self.token):
                raise ValueError('The current run does not own the browser')
            actions = state.setdefault('exchange_actions', {})
            if action_id in actions:
                raise ValueError('Existing submission or uncertainty requires read-only reconciliation')
            if kind != 'replies':
                used = sum(a['slot_id'] == self.slot['slot_id'] and a['kind'] == kind
                           and a['status'] != 'NOT_SUBMITTED' for a in actions.values())
                if used >= state['exchange_runs'][self.slot['slot_id']]['targets'][kind]:
                    raise ValueError('Original slot submission target is already consumed')
            actions[action_id] = {'kind': kind, 'target_id': target_id,
                                  'execution_id': self.execution_id, 'slot_id': self.slot['slot_id'],
                                  'night_id': self.slot['night_id'], 'status': 'UNCERTAIN',
                                  'attempted_at': now.isoformat()}
        _exchange_update(self.read, self.compare_and_swap, edit)
        # Recheck after ledger I/O: a delayed CAS cannot extend the action window.
        if exchange_slot(self.config, self.scheduled_at, self.clock())['phase'] != 'WORK':
            def not_submitted(state):
                state['exchange_actions'][action_id]['status'] = 'NOT_SUBMITTED'
            _exchange_update(self.read, self.compare_and_swap, not_submitted)
            raise ValueError('Work cutoff elapsed during intent persistence; no submission')
        proof = submit(datetime.fromisoformat(self.slot['work_cutoff']))
        reconcile_exchange_action(self.read, self.compare_and_swap, action_id, proof, self.clock())
        return proof

    def close(self, failed=False):
        """Finally cleanup uses fresh CAS snapshots and releases only this execution."""
        now = self.clock()

        def edit(state):
            record = state.get('exchange_runs', {}).get(self.slot['slot_id'])
            if not record or record.get('invocation_token') != self.token:
                return
            uncertain = any(a['execution_id'] == self.execution_id and a['status'] == 'UNCERTAIN'
                            for a in state.get('exchange_actions', {}).values())
            if record['status'] in {'RUNNING', 'WAITING'}:
                record.update(status=('FAILED' if failed else 'UNCONFIRMED' if uncertain
                                      else 'COMPLETED' if self.acquired else 'NOT_EXECUTED'),
                              ended_at=now.isoformat())
            record.setdefault('ended_at', now.isoformat())
            claim = state.get('browser_claim')
            if (claim and claim['execution_id'] == self.execution_id
                    and claim.get('invocation_token') == self.token):
                state['browser_claim'] = None
                state.setdefault('browser_releases', []).append(
                    {**claim, 'released_at': now.isoformat(), 'reason': 'OWN_FINALLY'})
        for attempt in range(3):
            try:
                _exchange_update(self.read, self.compare_and_swap, edit)
                self.acquired = False
                return
            except (OSError, RuntimeError):
                if attempt == 2:
                    raise  # Keep failure visible; never claim a successful release.


@contextmanager
def exchange_run(config, scheduled_at, execution_id, read, compare_and_swap,
                 execution_status, clock, targets):
    run = ExchangeRun(config, scheduled_at, execution_id, read, compare_and_swap,
                      execution_status, clock)
    failed = True
    try:
        run.acquire(targets)
        yield run
        failed = False
    finally:
        run.close(failed=failed)


def exchange_report(config, night, state, now):
    """All eleven slots, including missing past invocations; native receipts only."""
    start = datetime.combine(date.fromisoformat(night), datetime.min.time(), KST)
    start += timedelta(hours=22)
    seen, rows = set(), []
    for offset in range(11):
        slot = exchange_slot(config, start + timedelta(hours=offset), now)
        record = state.get('exchange_runs', {}).get(slot['slot_id'])
        counts = dict.fromkeys(('comments', 'likes', 'outgoing_requests', 'replies'), 0)
        uncertain = 0
        for action in state.get('exchange_actions', {}).values():
            if action['slot_id'] != slot['slot_id']:
                continue
            if action['status'] == 'NOT_SUBMITTED':
                continue
            if action['status'] != 'VERIFIED':
                uncertain += 1
                continue
            _exchange_receipt(action, action['proof'], now)
            identity = (action['kind'], action['proof']['native_id'])
            if identity not in seen:
                counts[action['kind']] += 1
                seen.add(identity)
        status = record['status'] if record else ('SCHEDULED' if slot['phase'] == 'EARLY'
                                                 else 'UNCONFIRMED')
        if status in {'RUNNING', 'WAITING'} and slot['phase'] == 'EXPIRED':
            status = 'UNCONFIRMED'
        rows.append({**slot, 'status': status, 'verified': counts, 'uncertain': uncertain,
                     'targets': record['targets'] if record else None,
                     **{key: record.get(key) if record else None for key in
                        ('execution_id', 'invoked_at', 'started_at', 'ended_at',
                         'reconciled_at', 'reason')}})
    return rows
