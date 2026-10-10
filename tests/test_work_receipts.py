"""Offline delivery evidence and once-only Work publication claims; no Naver access."""
import hashlib
import json
import sqlite3
from dataclasses import asdict, replace
from datetime import datetime

import pytest
from cryptography.fernet import Fernet

from blogbot.cloud import extract_bundle, filter_ready, import_manual_saves, pack
from blogbot.config import load_settings
from blogbot.core import (
    PostDraft,
    claim_publication,
    connect_db,
    import_work_receipts,
    save_post,
    validate_work_receipt,
)
from blogbot.planning import SaveDateRequired, record_verified_save, saved_count


@pytest.fixture
def setup(tmp_path, monkeypatch):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            stamp = datetime.fromisoformat('2026-10-12T12:00:00+09:00')
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)
    monkeypatch.setattr('blogbot.core.datetime', Fixed)
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('NAVER_BLOG_ID', 'test_owner')
    settings = load_settings()
    settings.artifact_dir.mkdir(parents=True)
    photo = settings.artifact_dir / 'generated-images' / 'question' / 'verified.png'
    photo.parent.mkdir(parents=True)
    photo.write_bytes(b'offline image fixture')
    post = PostDraft('parenting', '육아생활', 'question', '검증한 질문', '답입니다.\n\n## 조건\n\n설명입니다.',
                     [], ['https://example.org/evidence'], '2026-10-12', 28, 'APPROVED',
                     request_id='question', photos=[{'file': str(photo), 'sha256': hashlib.sha256(photo.read_bytes()).hexdigest()}],
                     provenance={'daily_plan': settings.config['daily_plan']})
    with connect_db(settings.db_path) as conn:
        ident = save_post(conn, post)
    packet = {'post': asdict(post), 'input': {'photos': []}, 'review': {'scores': [5, 5, 4, 4, 5, 5], 'total': 28,
               'decision': 'PASS', 'issues': [], 'blocking_issues': [], 'rewrite_instructions': '',
               'source_checks': [{'status': 'SUPPORTED', 'claim': 'offline', 'evidence': 'offline',
                                  'source_url': 'https://example.org/evidence'}]}}
    (settings.artifact_dir / f'2026-10-12-{ident:05d}.json').write_text(json.dumps(packet))
    (tmp_path / 'ready.json').write_text(json.dumps({'date': '2026-10-12',
        'daily_plan': settings.config['daily_plan'], 'posts': [{'id': ident, 'post': asdict(post)}]}))
    return settings


def record(**changes):
    return {'request_id': 'question', 'category': 'parenting', 'day': '2026-10-12',
            'status': 'SAVED_NAVER', **changes}


def published(**changes):
    return {**record(status='PUBLISHED', published_url='https://blog.naver.com/test_owner/123456',
                     post_id='123456', published_at='2026-10-12T11:00:00+09:00'), **changes}


def ingest(conn, *records):
    return import_work_receipts(conn, {'verified_date': '2026-10-12', 'records': list(records)},
                                blog_id='test_owner', categories={'parenting', 'exercise'})


def test_legacy_schema_migrates_without_fabricating_publication(tmp_path):
    path = tmp_path / 'legacy.db'
    with sqlite3.connect(path) as conn:
        conn.execute('CREATE TABLE save_receipts(request_id TEXT PRIMARY KEY,day TEXT NOT NULL,category TEXT NOT NULL)')
        conn.execute("INSERT INTO save_receipts VALUES('old','2026-10-05','parenting')")
    with connect_db(path) as conn:
        row = dict(conn.execute('SELECT * FROM save_receipts').fetchone())
        assert row['status'] == 'SAVED_NAVER'
        assert row['published_url'] == row['post_id'] == row['published_at'] == ''
        assert row['published_at_precision'] == 'second'
        assert row['save_check_json'] == '{}'


@pytest.mark.parametrize('changes', [
    {'published_url': ''}, {'post_id': ''}, {'published_at': ''},
    {'published_url': 'https://blog.naver.com/another_owner/123456'},
    {'published_url': 'https://blog.naver.com.evil.invalid/test_owner/123456'},
    {'published_url': 'http://blog.naver.com/test_owner/123456'},
    {'published_url': 'https://blog.naver.com/test_owner/123456?redirect=1'},
    {'post_id': '999999'}, {'published_at': '2026-10-12T11:00:00'},
    {'published_at': '2026-10-12T13:00:00+09:00'},
    {'published_at': '2026-10-11T11:00:00+09:00'},
    {'publish_attempted_at': '2026-10-12T11:30:00+09:00'},
])
def test_publication_requires_matching_observed_owner_url_and_time(setup, changes):
    with pytest.raises(ValueError):
        validate_work_receipt({**published(), **changes}, 'test_owner')


def test_publication_is_idempotent_and_cannot_be_reassigned_or_downgraded(setup):
    with connect_db(setup.db_path) as conn:
        ingest(conn, published())
        ingest(conn, {**published(), 'published_url': 'https://m.blog.naver.com/test_owner/123456'})
        assert conn.execute('SELECT status FROM posts').fetchone()[0] == 'PUBLISHED'
        assert saved_count(conn, setup.config['daily_plan']) == 1
        for bad in [record(), {**published(), 'post_id': '789', 'published_url': 'https://blog.naver.com/test_owner/789'},
                    record(status='PUBLISHING', publish_attempted_at='2026-10-12T11:00:00+09:00')]:
            with pytest.raises(ValueError):
                ingest(conn, bad)
        with pytest.raises(sqlite3.IntegrityError):
            ingest(conn, {**published(), 'request_id': 'different-question'})
        assert conn.execute('SELECT COUNT(*) FROM save_receipts').fetchone()[0] == 1


def test_prior_save_and_actual_publication_days_remain_separate(setup):
    with connect_db(setup.db_path) as conn:
        ingest(conn, published(day='2026-10-11'))
        assert conn.execute('SELECT day FROM save_receipts').fetchone()[0] == '2026-10-11'
        assert saved_count(conn, {**setup.config['daily_plan'], 'date': '2026-10-11'}) == 1
        assert saved_count(conn, setup.config['daily_plan']) == 1


def test_claim_is_once_only_and_unknown_outcome_survives_fresh_database(setup, tmp_path):
    with connect_db(setup.db_path) as conn:
        ingest(conn, record())
        claimed = claim_publication(conn, 'question', setup)
        assert claimed['status'] == 'PUBLICATION_CLAIMED'
        with pytest.raises(ValueError):
            claim_publication(conn, 'question', setup)
        pending = record(status='PUBLISH_UNCERTAIN', publish_attempted_at=claimed['publish_attempted_at'])
        ingest(conn, pending)
        with pytest.raises(ValueError):
            ingest(conn, record())
        with pytest.raises(ValueError):
            ingest(conn, {**pending, 'status': 'PUBLISHING'})
        with pytest.raises(SaveDateRequired):
            saved_count(conn, setup.config['daily_plan'])
    with connect_db(tmp_path / 'restored.db') as conn:
        ingest(conn, pending)
        with pytest.raises(SaveDateRequired):
            saved_count(conn, setup.config['daily_plan'])
        with pytest.raises(ValueError):
            claim_publication(conn, 'question', setup)


def test_confirmed_publication_resolves_unknown_without_resetting_attempt(setup):
    stamp = '2026-10-12T10:50:00+09:00'
    with connect_db(setup.db_path) as conn:
        ingest(conn, record(status='PUBLISH_UNCERTAIN', publish_attempted_at=stamp))
        ingest(conn, published())
        assert conn.execute('SELECT publish_attempted_at FROM save_receipts').fetchone()[0] == stamp
        assert saved_count(conn, setup.config['daily_plan']) == 1


@pytest.mark.parametrize('defect', ['no_post', 'no_receipt', 'stale', 'changed_text', 'failed_review', 'unsupported', 'photo_changed', 'reserved'])
def test_claim_requires_current_approved_packet_and_actual_save(setup, defect):
    with connect_db(setup.db_path) as conn:
        if defect != 'no_receipt':
            ingest(conn, record())
        if defect == 'no_post':
            conn.execute('DELETE FROM posts'); conn.commit()
        elif defect == 'stale':
            path = setup.db_path.parent / 'ready.json'
            payload = json.loads(path.read_text()); payload['date'] = '2026-10-11'; path.write_text(json.dumps(payload))
        elif defect == 'changed_text':
            conn.execute("UPDATE posts SET body='unreviewed edit'"); conn.commit()
        elif defect in {'failed_review', 'unsupported'}:
            path = next(setup.artifact_dir.glob('*.json'))
            payload = json.loads(path.read_text())
            if defect == 'failed_review':
                payload['review']['decision'] = 'REWRITE'
            else:
                payload['review']['source_checks'][0]['status'] = 'UNVERIFIED'
            path.write_text(json.dumps(payload))
        elif defect == 'photo_changed':
            (setup.artifact_dir / 'generated-images' / 'question' / 'verified.png').write_bytes(b'changed')
        elif defect == 'reserved':
            setup.config['operating_plan']['reservations']['2026-10-12'] = {'category': 'parenting', 'kind': 'owner_input_pending'}
        with pytest.raises(ValueError):
            claim_publication(conn, 'question', setup)
        assert not conn.execute("SELECT 1 FROM save_receipts WHERE status='PUBLISHING'").fetchone()


def test_empty_ledger_cannot_erase_persisted_unknown_outcome(setup):
    with connect_db(setup.db_path) as conn:
        ingest(conn, record(status='PUBLISH_UNCERTAIN', publish_attempted_at='2026-10-12T11:00:00+09:00'))
    filter_ready(setup.db_path.parent, {'verified_date': '2026-10-12', 'records': []},
                 setup.config['daily_plan'], setup)
    assert json.loads((setup.db_path.parent / 'ready.json').read_text())['posts'] == []


def test_old_published_receipt_without_evidence_is_held(setup):
    with pytest.raises(ValueError):
        filter_ready(setup.db_path.parent, {'verified_date': '2026-10-12',
                     'records': [record(status='PUBLISHED')]}, setup.config['daily_plan'])


@pytest.mark.parametrize('terminal', ['PUBLISHED', 'SAVE_NOT_SAVED'])
def test_legacy_saved_import_never_downgrades_finished_delivery(setup, tmp_path, terminal):
    with connect_db(setup.db_path) as conn:
        ingest(conn, published() if terminal == 'PUBLISHED' else not_saved())
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'manual-saves.json').write_text(json.dumps({'records': [record()]}))
    import_manual_saves(replace(setup, root=tmp_path))
    with connect_db(setup.db_path) as conn:
        assert conn.execute('SELECT status FROM attempts').fetchone()[0] == terminal


@pytest.mark.parametrize('status', ['SAVING', 'SAVE_UNCERTAIN'])
def test_legacy_confirmed_save_resolves_uncertain_receipt(setup, tmp_path, status):
    with connect_db(setup.db_path) as conn:
        ingest(conn, record(status=status))
    (tmp_path / 'config').mkdir()
    (tmp_path / 'config' / 'manual-saves.json').write_text(json.dumps({'records': [record()]}))
    import_manual_saves(replace(setup, root=tmp_path))
    with connect_db(setup.db_path) as conn:
        for table in ('posts', 'attempts', 'save_receipts'):
            assert conn.execute(f'SELECT status FROM {table}').fetchone()[0] == 'SAVED_NAVER'
        assert saved_count(conn, setup.config['daily_plan']) == 1


def test_batch_conflict_rolls_back_and_attempt_generation_day_is_preserved(setup):
    with connect_db(setup.db_path) as conn:
        conn.execute("INSERT INTO attempts(day,category,request_id,status) VALUES('2026-10-01','parenting','question','APPROVED')")
        conn.commit()
        ingest(conn, record())
        assert conn.execute('SELECT day FROM attempts').fetchone()[0] == '2026-10-01'
        with pytest.raises(ValueError):
            ingest(conn, record(request_id='other'), {**published(), 'category': 'exercise'})
        assert conn.execute('SELECT COUNT(*) FROM save_receipts').fetchone()[0] == 1


@pytest.mark.parametrize('precision,attempt,valid', [
    ('minute', '2026-10-12T11:00:45+09:00', True),
    ('second', '2026-10-12T11:00:45+09:00', False),
    ('minute', '2026-10-12T11:01:00+09:00', False),
    ('day', '2026-10-12T11:00:00+09:00', False),
])
def test_observed_minute_precision_preserves_displayed_time(setup, precision, attempt, valid):
    evidence = published(published_at_precision=precision, publish_attempted_at=attempt)
    with connect_db(setup.db_path) as conn:
        if not valid:
            with pytest.raises(ValueError):
                ingest(conn, evidence)
            return
        ingest(conn, evidence)
        persisted = dict(conn.execute('SELECT * FROM save_receipts').fetchone())
        assert persisted['published_at'] == '2026-10-12T11:00:00+09:00'
        assert persisted['published_at_precision'] == 'minute'
        ingest(conn, persisted)
        assert validate_work_receipt(persisted, 'test_owner')['published_at_precision'] == 'minute'


def test_minute_precision_does_not_invent_seconds(setup):
    with pytest.raises(ValueError):
        validate_work_receipt(published(published_at_precision='minute',
                                       published_at='2026-10-12T11:00:23+09:00'), 'test_owner')


@pytest.mark.parametrize('status', ['PUBLISHED', 'PUBLISHING', 'PUBLISH_UNCERTAIN'])
def test_saved_resolution_cannot_downgrade_publication(setup, status):
    with connect_db(setup.db_path) as conn:
        evidence = published() if status == 'PUBLISHED' else record(
            status=status, publish_attempted_at='2026-10-12T11:00:00+09:00')
        ingest(conn, evidence)
        post_id = conn.execute('SELECT id FROM posts').fetchone()[0]
        with pytest.raises(ValueError):
            record_verified_save(conn, post_id, {'day': '2026-10-12'})
        assert conn.execute('SELECT status FROM posts').fetchone()[0] == status


def test_new_unpack_preserves_saved_publication_candidate_but_never_repeats_claim(setup, tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    archive = tmp_path / 'prepared.enc'
    pack(setup, archive)
    target = tmp_path / 'work'
    restored = replace(setup, db_path=target / 'blog.db', artifact_dir=target / 'drafts', inbox_dir=target / 'inbox')

    def unpack_again(records):
        extract_bundle(archive.read_bytes(), target, key)
        with connect_db(restored.db_path) as conn:
            ingest(conn, *records)
        filter_ready(target, {'verified_date': '2026-10-12', 'records': records},
                     setup.config['daily_plan'], restored)
        return json.loads((target / 'ready.json').read_text())

    first = unpack_again([])
    assert len(first['posts']) == 1 and first['publication_ready'] == []
    with connect_db(restored.db_path) as conn:
        ingest(conn, record())  # Work observed the actual save, then stopped before publishing.
    resumed = unpack_again([record()])
    assert resumed['posts'] == [] and len(resumed['publication_ready']) == 1
    with connect_db(restored.db_path) as conn:
        claimed = claim_publication(conn, 'question', restored)
    # A stale external saved-only ledger must not rewind the claim during DB restoration.
    with pytest.raises(ValueError):
        unpack_again([record()])
    with connect_db(restored.db_path) as conn:
        persisted = conn.execute('SELECT * FROM save_receipts').fetchone()
        assert persisted['status'] == 'PUBLISHING'
        assert persisted['publish_attempted_at'] == claimed['publish_attempted_at']
        with pytest.raises(ValueError):
            claim_publication(conn, 'question', restored)
    pending = record(status='PUBLISHING', publish_attempted_at=claimed['publish_attempted_at'])
    interrupted = unpack_again([pending])
    assert interrupted['posts'] == interrupted['publication_ready'] == []
    with connect_db(restored.db_path) as conn, pytest.raises(ValueError):
        claim_publication(conn, 'question', restored)


@pytest.mark.parametrize('state', ['SAVING', 'SAVE_UNCERTAIN', 'SAVED_NAVER', 'SAVE_NOT_SAVED', 'PUBLISHING',
                                  'PUBLISH_UNCERTAIN', 'PUBLISHED', 'PUBLICATION_NOT_PUBLISHED'])
def test_restore_preserves_every_existing_work_receipt(setup, tmp_path, monkeypatch, state):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    archive = tmp_path / 'old.enc'
    pack(setup, archive)
    target = tmp_path / 'work'
    extract_bundle(archive.read_bytes(), target, key)
    evidence = (not_saved() if state == 'SAVE_NOT_SAVED' else published() if state == 'PUBLISHED' else not_published()
                if state == 'PUBLICATION_NOT_PUBLISHED' else record(status=state,
                    **({'publish_attempted_at': '2026-10-12T10:00:00+09:00'}
                       if state.startswith('PUBLISH') else {})))
    with connect_db(target / 'blog.db') as conn:
        ingest(conn, evidence)
        before = dict(conn.execute('SELECT * FROM save_receipts').fetchone())
    extract_bundle(archive.read_bytes(), target, key)
    with connect_db(target / 'blog.db') as conn:
        assert dict(conn.execute('SELECT * FROM save_receipts').fetchone()) == before
        assert conn.execute('SELECT status FROM posts').fetchone()[0] == state
        assert conn.execute('SELECT status FROM attempts').fetchone()[0] == state


def test_restore_receipt_conflict_preserves_original_database_and_handoff(setup, tmp_path, monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    with connect_db(setup.db_path) as conn:
        ingest(conn, published())
    archive = tmp_path / 'published.enc'
    pack(setup, archive)
    target = tmp_path / 'work'
    target.mkdir()
    with connect_db(target / 'blog.db') as conn:
        ingest(conn, published(post_id='789', published_url='https://blog.naver.com/test_owner/789'))
    old_database = (target / 'blog.db').read_bytes()
    handoff = target / 'ready.json'
    handoff.write_text('original private handoff')
    with pytest.raises(ValueError):
        extract_bundle(archive.read_bytes(), target, key)
    assert (target / 'blog.db').read_bytes() == old_database
    assert handoff.read_text() == 'original private handoff'


@pytest.mark.parametrize('broken', ['garbage', 'missing_table'])
def test_restore_damaged_existing_database_is_not_treated_as_no_receipts(setup, tmp_path, monkeypatch, broken):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    archive = tmp_path / 'prepared.enc'
    pack(setup, archive)
    target = tmp_path / 'work'
    target.mkdir()
    database = target / 'blog.db'
    if broken == 'garbage':
        database.write_bytes(b'damaged local private Work database')
    else:
        with sqlite3.connect(database) as conn:
            conn.execute('CREATE TABLE unrelated(value TEXT)')
    original = database.read_bytes()
    with pytest.raises(sqlite3.DatabaseError):
        extract_bundle(archive.read_bytes(), target, key)
    assert database.read_bytes() == original
    assert not (target / 'ready.json').exists()


@pytest.mark.parametrize('state', ['SAVING', 'SAVE_UNCERTAIN'])
def test_claim_rechecks_uncertain_save_imported_after_handoff(setup, state):
    with connect_db(setup.db_path) as conn:
        ingest(conn, record(), record(request_id='older', day='2026-10-11', status=state))
        with pytest.raises(ValueError, match='uncertain'):
            claim_publication(conn, 'question', setup)
        assert conn.execute("SELECT status FROM save_receipts WHERE request_id='question'").fetchone()[0] == 'SAVED_NAVER'


def not_published(**changes):
    return record(status='PUBLICATION_NOT_PUBLISHED', publish_attempted_at='2026-10-12T10:00:00+09:00',
                  publication_check={'checked_at': '2026-10-12T11:00:00+09:00',
                     'published_list_url': 'https://blog.naver.com/test_owner',
                     'published_list_checked': True, 'draft_still_saved': True,
                     'evidence_ref': 'private-observation-123', **changes})


def test_verified_failed_publication_releases_only_global_uncertainty_and_preserves_budget(setup, tmp_path):
    with connect_db(setup.db_path) as conn:
        ingest(conn, record(status='PUBLISH_UNCERTAIN', publish_attempted_at='2026-10-12T10:00:00+09:00'))
        ingest(conn, not_published())
        assert saved_count(conn, setup.config['daily_plan']) == 1
        assert saved_count(conn, {**setup.config['daily_plan'], 'date': '2026-10-13'}) == 0
        with pytest.raises(ValueError):
            claim_publication(conn, 'question', setup)
        with pytest.raises(ValueError):
            ingest(conn, record())
        terminal = dict(conn.execute('SELECT * FROM save_receipts').fetchone())
        assert terminal['publish_attempted_at'] == '2026-10-12T10:00:00+09:00'
    with connect_db(tmp_path / 'recreated.db') as conn:
        ingest(conn, terminal)
        assert saved_count(conn, {**setup.config['daily_plan'], 'date': '2026-10-13'}) == 0
        assert json.loads(conn.execute('SELECT publication_check_json FROM save_receipts').fetchone()[0])['draft_still_saved']


@pytest.mark.parametrize('changes', [
    {'checked_at': ''}, {'checked_at': '2026-10-12T11:00:00'},
    {'checked_at': '2026-10-12T09:00:00+09:00'}, {'checked_at': '2026-10-12T13:00:00+09:00'},
    {'published_list_checked': False}, {'draft_still_saved': False}, {'evidence_ref': ''},
    {'published_list_url': 'https://blog.naver.com/another_owner'},
])
def test_failed_publication_requires_post_attempt_owner_observation(setup, changes):
    with connect_db(setup.db_path) as conn:
        ingest(conn, record(status='PUBLISH_UNCERTAIN', publish_attempted_at='2026-10-12T10:00:00+09:00'))
        with pytest.raises(ValueError):
            ingest(conn, not_published(**changes))
        assert conn.execute('SELECT status FROM save_receipts').fetchone()[0] == 'PUBLISH_UNCERTAIN'


def test_failed_publication_cannot_reset_attempt_or_downgrade_published(setup):
    with connect_db(setup.db_path) as conn:
        ingest(conn, record(status='PUBLISH_UNCERTAIN', publish_attempted_at='2026-10-12T09:00:00+09:00'))
        with pytest.raises(ValueError):
            ingest(conn, not_published())
        ingest(conn, published())
        with pytest.raises(ValueError):
            ingest(conn, not_published())


def not_saved(**changes):
    return record(status='SAVE_NOT_SAVED', save_check={
        'resolution': 'not-saved', 'attempted_at': '2026-10-12T10:00:00+09:00',
        'checked_at': '2026-10-12T11:00:00+09:00', 'blog_url': 'https://blog.naver.com/test_owner',
        'draft_list_checked': True, 'published_list_checked': True, 'draft_absent': True,
        'published_absent': True, 'evidence_ref': 'private-save-observation-123', **changes})


@pytest.mark.parametrize('state', ['SAVING', 'SAVE_UNCERTAIN'])
def test_verified_saved_resolution_updates_receipt_atomically(setup, state):
    with connect_db(setup.db_path) as conn:
        ingest(conn, record(status=state))
        ident = conn.execute('SELECT id FROM posts').fetchone()[0]
        conn.execute("CREATE TRIGGER reject_post_update BEFORE UPDATE ON posts BEGIN SELECT RAISE(ABORT,'offline failure'); END")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError):
            record_verified_save(conn, ident, {'day': '2026-10-12'})
        assert conn.execute('SELECT status FROM save_receipts').fetchone()[0] == state
        conn.execute('DROP TRIGGER reject_post_update'); conn.commit()
        record_verified_save(conn, ident, {'day': '2026-10-12'})
        assert conn.execute('SELECT status FROM save_receipts').fetchone()[0] == 'SAVED_NAVER'
        assert conn.execute('SELECT status FROM posts').fetchone()[0] == 'SAVED_NAVER'
        assert conn.execute('SELECT status FROM attempts').fetchone()[0] == 'SAVED_NAVER'
        assert saved_count(conn, setup.config['daily_plan']) == 1
        claim_publication(conn, 'question', setup)


@pytest.mark.parametrize('outcome', ['not-saved', 'discard'])
def test_cli_absence_resolution_releases_only_global_hold(setup, monkeypatch, outcome):
    from blogbot.cli import main
    with connect_db(setup.db_path) as conn:
        ingest(conn, record(status='SAVE_UNCERTAIN'))
        ident = conn.execute('SELECT id FROM posts').fetchone()[0]
    base = ['blogbot', 'resolve', '--id', str(ident), '--outcome', outcome]
    monkeypatch.setattr('sys.argv', base)
    with pytest.raises(SystemExit):
        main()  # No evidence must leave the hold intact.
    evidence = not_saved(resolution=outcome)
    proof = setup.db_path.parent / 'absence.json'
    proof.write_text(json.dumps({'verified_date': '2026-10-12', 'day': evidence['day'],
                                 'save_check': evidence['save_check']}))
    monkeypatch.setattr('sys.argv', base + ['--evidence-file', str(proof)])
    main()
    with connect_db(setup.db_path) as conn:
        terminal = dict(conn.execute('SELECT * FROM save_receipts').fetchone())
        assert terminal['status'] == 'SAVE_NOT_SAVED'
        assert conn.execute('SELECT status FROM posts').fetchone()[0] == 'SAVE_NOT_SAVED'
        assert conn.execute('SELECT status FROM attempts').fetchone()[0] == 'SAVE_NOT_SAVED'
        assert saved_count(conn, setup.config['daily_plan']) == 1
        assert saved_count(conn, {**setup.config['daily_plan'], 'date': '2026-10-13'}) == 0
        for old in [record(), record(status='SAVING'), published()]:
            with pytest.raises(ValueError):
                ingest(conn, old)
        with pytest.raises(ValueError):
            record_verified_save(conn, ident, {'day': '2026-10-12'})
        with pytest.raises(ValueError):
            claim_publication(conn, 'question', setup)
    with connect_db(setup.db_path.parent / 'recreated.db') as conn:
        ingest(conn, terminal)
        assert saved_count(conn, setup.config['daily_plan']) == 1
    filter_ready(setup.db_path.parent, {'verified_date': '2026-10-12', 'records': [terminal]},
                 setup.config['daily_plan'], setup)
    ready = json.loads((setup.db_path.parent / 'ready.json').read_text())
    assert ready['posts'] == ready['publication_ready'] == []


@pytest.mark.parametrize('changes', [
    {'attempted_at': ''}, {'checked_at': '2026-10-12T09:00:00+09:00'},
    {'attempted_at': '2026-10-11T10:00:00+09:00'}, {'checked_at': '2026-10-12T13:00:00+09:00'},
    {'draft_list_checked': False}, {'published_list_checked': False}, {'draft_absent': False},
    {'published_absent': False}, {'evidence_ref': ''}, {'blog_url': 'https://blog.naver.com/wrong_owner'},
])
def test_absence_resolution_requires_actual_owner_observation(setup, changes):
    with connect_db(setup.db_path) as conn:
        ingest(conn, record(status='SAVE_UNCERTAIN'))
        with pytest.raises(ValueError):
            ingest(conn, not_saved(**changes))
        assert conn.execute('SELECT status FROM save_receipts').fetchone()[0] == 'SAVE_UNCERTAIN'


@pytest.mark.parametrize('prior', ['SAVED_NAVER', 'PUBLISHING', 'PUBLISH_UNCERTAIN', 'PUBLISHED', 'PUBLICATION_NOT_PUBLISHED'])
def test_absence_resolution_never_downgrades_observed_delivery(setup, prior):
    with connect_db(setup.db_path) as conn:
        evidence = (published() if prior == 'PUBLISHED' else not_published()
                    if prior == 'PUBLICATION_NOT_PUBLISHED' else record(status=prior,
                        **({'publish_attempted_at': '2026-10-12T10:00:00+09:00'}
                           if prior.startswith('PUBLISH') else {})))
        ingest(conn, evidence)
        with pytest.raises(ValueError):
            ingest(conn, not_saved())
        assert conn.execute('SELECT status FROM save_receipts').fetchone()[0] == prior
