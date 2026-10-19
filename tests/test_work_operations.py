"""Separate manual Work lineage, once-only starts and encrypted restoration."""
import copy
import hashlib
import io
import json
import zipfile
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from cryptography.fernet import Fernet

from blogbot import core, pipeline
from blogbot import work_operations as work
from blogbot.cloud import extract_bundle, pack
from blogbot.config import load_settings
from blogbot.core import connect_db
from blogbot.inputs import ContentRequest


@pytest.fixture
def state(tmp_path, monkeypatch):
    clock = {'now': datetime.fromisoformat('2026-10-13T09:00:00+09:00')}

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            stamp = clock['now']
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)

    monkeypatch.setattr(core, 'datetime', Clock)
    monkeypatch.setattr(work, 'datetime', Clock)
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path / 'private'))
    monkeypatch.setenv('NAVER_BLOG_ID', 'test_owner')
    settings = load_settings()
    with connect_db(settings.db_path):
        pass
    monkeypatch.setattr(pipeline, 'BlogLLM', lambda *a, **k: pytest.fail('No paid calls'))
    monkeypatch.setattr(pipeline, 'make_writer', lambda *a: pytest.fail('No browser writes'))
    return settings, clock


def manifest(*, action='create', category='origins', operation='manual-test', request='owner-request'):
    return {'version': work.VERSION, 'operation_id': operation, 'request_id': request,
            'category': category, 'action': action, 'approval_reference': 'Sentinel_explicit_test_request',
            'approved_by': 'repository_owner', 'approved_date': '2026-10-13', 'scope': 'draft_only',
            'target_sha256': '' if action == 'create' else 'a' * 64}


def result(clock, *, outcome='COMPLETED'):
    now = clock['now'].isoformat()
    value = {'outcome': outcome, 'content_sha256': 'a' * 64,
             'evidence_ref': 'private-observed-fixture', 'checked_at': now}
    if outcome == 'SAVED_NAVER':
        value['saved_at'] = now
    return value


@pytest.mark.parametrize('category', sorted(work.CATEGORIES))
def test_manual_categories_do_not_consume_or_reset_automatic_tables(state, category):
    settings, clock = state
    before = settings.db_path.read_bytes()
    packet = manifest(category=category)
    assert work.register(settings, packet)['status'] == 'REGISTERED'
    assert work.start(settings, packet['operation_id'])['scope'] == 'draft_only'
    clock['now'] += timedelta(minutes=1)
    assert work.finish(settings, packet['operation_id'], result(clock))['status'] == 'COMPLETED'
    assert work.exclusions(settings) == {packet['request_id']}
    assert settings.db_path.read_bytes() == before
    assert work.register(settings, packet)['cached'] is True
    with pytest.raises(ValueError, match='already started'):
        work.start(settings, packet['operation_id'])


def test_changed_approval_or_completed_result_cannot_be_replayed(state):
    settings, clock = state
    packet = manifest()
    work.register(settings, packet)
    with pytest.raises(ValueError, match='cannot be replaced'):
        work.register(settings, {**packet, 'request_id': 'replacement'})
    work.start(settings, packet['operation_id'])
    completed = result(clock)
    work.finish(settings, packet['operation_id'], completed)
    assert work.finish(settings, packet['operation_id'], completed)['cached'] is True
    with pytest.raises(ValueError, match='cannot be reset'):
        work.finish(settings, packet['operation_id'], {**completed, 'content_sha256': 'b' * 64})
    with pytest.raises(ValueError, match='created again'):
        work.register(settings, {**packet, 'operation_id': 'new-operation'})


@pytest.mark.parametrize('defect', ['publish', 'automatic', 'future', 'unapproved', 'target', 'extra'])
def test_scope_and_date_are_not_inferred_from_a_plausible_request(state, defect):
    settings, _ = state
    packet = manifest()
    if defect == 'publish':
        packet['action'] = 'publish'
    elif defect == 'automatic':
        packet['scope'] = 'automatic'
    elif defect == 'future':
        packet['approved_date'] = '2026-10-14'
    elif defect == 'unapproved':
        packet['approved_by'] = 'generated_text'
    elif defect == 'target':
        packet['action'] = 'revise'
    else:
        packet['body'] = 'Manuscript content does not belong in this metadata journal'
    with pytest.raises(ValueError):
        work.register(settings, packet)
    assert work.records(settings.db_path.parent) == []


def test_midnight_blocks_new_start_but_keeps_observed_completion(state):
    settings, clock = state
    packet = manifest()
    work.register(settings, packet)
    clock['now'] += timedelta(days=1)
    with pytest.raises(ValueError, match='not valid'):
        work.start(settings, packet['operation_id'])
    clock['now'] -= timedelta(days=1)
    work.start(settings, packet['operation_id'])
    clock['now'] += timedelta(days=1)
    work.finish(settings, packet['operation_id'], result(clock))
    record = work.records(settings.db_path.parent)[0][1]
    assert record['manifest']['approved_date'] == '2026-10-13'
    assert record['result']['checked_at'].startswith('2026-10-14')


def test_changed_target_or_other_active_operation_blocks_a_new_write(state):
    settings, _ = state
    packet = manifest(action='revise')
    work.register(settings, packet)
    with pytest.raises(ValueError, match='target content changed'):
        work.start(settings, packet['operation_id'], 'b' * 64)
    work.start(settings, packet['operation_id'], packet['target_sha256'])
    other = manifest(operation='second', request='second-request')
    work.register(settings, other)
    with pytest.raises(ValueError, match='existing manual operation'):
        work.start(settings, other['operation_id'])
    assert pipeline.run_daily(settings, save_to_naver=True)[0]['reason'] == 'unresolved_manual_work'
    assert pipeline.save_pending(settings)[0]['reason'] == 'unresolved_manual_work'
    with connect_db(settings.db_path) as conn, pytest.raises(ValueError, match='Owner-only publication'):
        core.claim_publication(conn, 'unrelated', settings)


def test_uncertain_manual_save_is_reconciled_without_restarting_or_refilling_quota(state):
    settings, clock = state
    packet = manifest(action='save')
    work.register(settings, packet)
    work.start(settings, packet['operation_id'], packet['target_sha256'])
    clock['now'] += timedelta(minutes=1)
    work.finish(settings, packet['operation_id'], result(clock, outcome='SAVE_UNCERTAIN'))
    assert work.pending_writes(settings) == [packet['operation_id']]
    with pytest.raises(ValueError, match='already started'):
        work.start(settings, packet['operation_id'], packet['target_sha256'])
    clock['now'] += timedelta(minutes=1)
    work.finish(settings, packet['operation_id'], result(clock, outcome='SAVED_NAVER'))
    assert work.pending_writes(settings) == []
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM save_receipts').fetchone()[0] == 0
    assert work.exclusions(settings) == {packet['request_id']}


def test_manual_receipt_cannot_be_misrouted_into_ordinary_daily_quota(state):
    settings, _ = state
    with connect_db(settings.db_path) as conn:
        with pytest.raises(ValueError, match='automatic quota'):
            core.import_work_receipts(conn, {'verified_date': '2026-10-13', 'records': [{
                'operation_id': 'manual-test', 'request_id': 'owner-request', 'category': 'origins',
                'day': '2026-10-13', 'status': 'SAVED_NAVER'}]}, blog_id='test_owner', categories=work.CATEGORIES)
        assert conn.execute('SELECT COUNT(*) FROM save_receipts').fetchone()[0] == 0


def test_registered_manual_question_does_not_enter_automatic_paid_preparation(state, monkeypatch):
    settings, _ = state
    packet = manifest()
    work.register(settings, packet)
    request = ContentRequest(packet['request_id'], 'origins', {'question': 'Actual owner question'})
    monkeypatch.setattr(pipeline, 'collect_requests', lambda *a: ([request], []))
    pipeline.run_daily(settings)
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0


def test_observed_manual_publication_still_consumes_the_shared_publication_day(state):
    settings, clock = state
    packet = manifest()
    work.register(settings, packet)
    work.start(settings, packet['operation_id'])
    work.finish(settings, packet['operation_id'], result(clock))
    clock['now'] += timedelta(minutes=1)
    with connect_db(settings.db_path) as conn:
        core.import_work_receipts(conn, {'verified_date': '2026-10-13', 'records': [{
            'mode': 'manual', 'operation_id': packet['operation_id'],
            'request_id': packet['request_id'], 'category': 'origins', 'day': '2026-10-13',
            'status': 'PUBLISHED', 'published_url': 'https://blog.naver.com/test_owner/123456',
            'post_id': '123456', 'published_at': clock['now'].isoformat()}]},
            blog_id='test_owner', categories=work.CATEGORIES)
        from blogbot.planning import saved_count
        assert saved_count(conn, settings.config['daily_plan']) == 1
    assert pipeline.run_daily(settings)[0]['status'] == 'DAILY_PLAN_LIMIT'


def test_old_bundle_preserves_local_start_and_completion_across_fresh_restore(state, tmp_path, monkeypatch):
    settings, clock = state
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    packet = manifest(action='save')
    work.register(settings, packet)
    old = tmp_path / 'handoff' / 'old.enc'
    pack(settings, old)
    work.start(settings, packet['operation_id'], packet['target_sha256'])
    extract_bundle(old.read_bytes(), settings.db_path.parent, key)
    assert work.records(settings.db_path.parent)[0][1]['status'] == 'STARTED'
    clock['now'] += timedelta(minutes=1)
    work.finish(settings, packet['operation_id'], result(clock, outcome='SAVED_NAVER'))
    extract_bundle(old.read_bytes(), settings.db_path.parent, key)
    assert work.records(settings.db_path.parent)[0][1]['status'] == 'SAVED_NAVER'
    current = tmp_path / 'handoff' / 'current.enc'
    pack(settings, current)
    restored = tmp_path / 'fresh'
    extract_bundle(current.read_bytes(), restored, key)
    relocated = replace(settings, db_path=restored / 'blog.db')
    assert work.exclusions(relocated) == {packet['request_id']}
    assert work.pending_writes(relocated) == []
    with pytest.raises(ValueError, match='already started'):
        work.start(relocated, packet['operation_id'], packet['target_sha256'])


def test_conflicting_completed_journal_never_rewinds_local_database(state, tmp_path, monkeypatch):
    settings, clock = state
    packet = manifest()
    work.register(settings, packet)
    work.start(settings, packet['operation_id'])
    work.finish(settings, packet['operation_id'], result(clock))
    local = work.records(settings.db_path.parent)[0][1]
    incoming = copy.deepcopy(local)
    incoming['result']['content_sha256'] = 'b' * 64
    key = Fernet.generate_key()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key.decode())
    bundle = tmp_path / 'handoff' / 'current.enc'
    pack(settings, bundle)
    changed = io.BytesIO()
    with (zipfile.ZipFile(io.BytesIO(Fernet(key).decrypt(bundle.read_bytes()))) as source,
          zipfile.ZipFile(changed, 'w') as archive):
        for name in source.namelist():
            archive.writestr(name, json.dumps(incoming) if name.startswith('work-operations/')
                             else source.read(name))
    before = hashlib.sha256(settings.db_path.read_bytes()).hexdigest()
    with pytest.raises(ValueError, match='Conflicting manual Work outcomes'):
        extract_bundle(Fernet(key).encrypt(changed.getvalue()), settings.db_path.parent, key.decode())
    assert hashlib.sha256(settings.db_path.read_bytes()).hexdigest() == before


def test_tampered_journal_blocks_automatic_work_instead_of_assuming_no_history(state):
    settings, _ = state
    work.register(settings, manifest())
    path, record = work.records(settings.db_path.parent)[0]
    record['manifest']['request_id'] = 'tampered'
    path.write_text(json.dumps(record))
    with pytest.raises(ValueError, match='integrity mismatch'):
        pipeline.run_daily(settings)


def test_absence_resolution_requires_actual_owner_lists_and_keeps_the_original_start(state):
    settings, clock = state
    packet = manifest(action='save')
    work.register(settings, packet)
    work.start(settings, packet['operation_id'], packet['target_sha256'])
    started = clock['now'].isoformat()
    clock['now'] += timedelta(minutes=1)
    work.finish(settings, packet['operation_id'], result(clock, outcome='SAVE_UNCERTAIN'))
    clock['now'] += timedelta(minutes=1)
    observed = {**result(clock, outcome='SAVE_NOT_SAVED'), 'save_check': {
        'resolution': 'not-saved', 'attempted_at': started, 'checked_at': clock['now'].isoformat(),
        'blog_url': 'https://blog.naver.com/test_owner', 'draft_list_checked': True,
        'published_list_checked': True, 'draft_absent': True, 'published_absent': True,
        'evidence_ref': 'private-observed-absence-fixture'}}
    wrong_owner = copy.deepcopy(observed)
    wrong_owner['save_check']['blog_url'] = 'https://blog.naver.com/other_owner'
    with pytest.raises(ValueError):
        work.finish(settings, packet['operation_id'], wrong_owner)
    assert work.pending_writes(settings)
    work.finish(settings, packet['operation_id'], observed)
    assert work.pending_writes(settings) == []
    assert work.records(settings.db_path.parent)[0][1]['started_at'] == started
    with pytest.raises(ValueError):
        work.start(settings, packet['operation_id'], packet['target_sha256'])
