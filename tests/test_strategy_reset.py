"""Focused execution, no duplicate API work, and real publication caps."""
import hashlib
import json
import os
import tomllib
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from blogbot import cloud
from blogbot.config import load_settings
from blogbot.core import (
    PostDraft,
    claim_publication,
    connect_db,
    import_work_receipts,
    publication_days,
    save_post,
)
from blogbot.inputs import ContentRequest, enqueue_file
from blogbot.life_economics import post_contract, request_contract
from blogbot.pipeline import run_daily, save_pending
from blogbot.planning import resolve_plan


def settings_at(tmp_path, monkeypatch, day):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            stamp = datetime.fromisoformat(day + 'T12:00:00+09:00')
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)
    monkeypatch.setattr('blogbot.core.datetime', Fixed)
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('NAVER_BLOG_ID', 'test_owner')
    return load_settings()


@pytest.mark.parametrize('day,phase,target,category', [
    ('2026-10-15', 'audit', 0, 'parenting'),
    ('2026-10-16', 'audit', 1, 'parenting'),
    ('2026-10-17', 'pilot', 1, 'parenting'),
    ('2026-10-21', 'pilot', 1, 'investment'),
    ('2026-10-22', 'focused', 0, 'parenting'),
    ('2026-10-23', 'focused', 1, 'investment'),
    ('2026-10-24', 'focused', 1, 'parenting'),
    ('2026-10-28', 'focused', 1, 'investment'),
])
def test_active_phase_and_publication_slots(tmp_path, monkeypatch, day, phase, target, category):
    settings = settings_at(tmp_path, monkeypatch, day)
    plan = settings.config['daily_plan']
    assert (plan['phase'], plan['target'], plan['category']) == (phase, target, category)
    assert plan['topic_selection'] == 'autonomous_verified'
    assert settings.daily_count == target
    assert sum(c['max_daily'] for c in settings.config['categories'].values()) == target
    assert settings.config['community']['enabled'] is False


def test_history_is_preserved_and_review_does_not_restore_old_categories():
    path = Path(__file__).parents[1] / 'config/blog.toml'
    config = tomllib.loads(path.read_text())
    assert resolve_plan(config, date(2026, 10, 14))['version'] == 'weekly-2221-v1'
    days = [date(2026, 10, 26) + timedelta(days=i) for i in range(7)]
    plans = [resolve_plan(config, d) for d in days]
    assert sum(p['target'] for p in plans) == 5
    assert [p['category'] for p in plans if p['target']] == [
        'parenting', 'parenting', 'investment', 'investment', 'parenting']
    assert resolve_plan(config, date(2026, 11, 12))['review_due'] is True
    assert resolve_plan(config, date(2026, 11, 12))['version'] == 'strategy-reset-v2'


def test_five_per_week_cycle_preserves_two_to_one_topic_mix():
    config = tomllib.loads((Path(__file__).parents[1] / 'config/blog.toml').read_text())
    counts = []
    for week in range(6):
        plans = [resolve_plan(config, date(2026, 10, 26) + timedelta(days=7 * week + day))
                 for day in range(7)]
        assert sum(p['target'] for p in plans) == 5
        assert all(p['target'] in {0, 1} and p['weekly_publication_limit'] == 5 for p in plans)
        counts.append(tuple(sum(p['target'] for p in plans if p['category'] == category)
                            for category in ('parenting', 'investment')))
    assert counts == [(3, 2), (4, 1), (3, 2)] * 2
    assert tuple(sum(pair[i] for pair in counts[:3]) for i in range(2)) == (10, 5)


@pytest.mark.parametrize('limit', [3, 10, 20])
def test_weekly_limit_cannot_silently_change_approved_capacity(limit):
    config = tomllib.loads((Path(__file__).parents[1] / 'config/blog.toml').read_text())
    config['strategy_reset']['weekly_publication_limit'] = limit
    with pytest.raises(ValueError, match='Invalid focused Work strategy'):
        resolve_plan(config, date(2026, 10, 26))


@pytest.mark.parametrize('field,value', [
    ('start_date', '2026-10-19'), ('end_date', '2026-10-25'),
    ('publication_limit', 17), ('publication_limit', 16.0), ('extra_limit', 20),
])
def test_transition_exception_cannot_expand_approved_dates_or_limit(field, value):
    config = tomllib.loads((Path(__file__).parents[1] / 'config/blog.toml').read_text())
    config['strategy_reset']['transition_week'][field] = value
    with pytest.raises(ValueError, match='approved Oct 12-18 transition'):
        resolve_plan(config, date(2026, 10, 16))


def test_transition_week_does_not_reset_counts_or_extend_past_october_18():
    config = tomllib.loads((Path(__file__).parents[1] / 'config/blog.toml').read_text())
    assert resolve_plan(config, date(2026, 10, 16))['weekly_publication_limit'] == 16
    assert resolve_plan(config, date(2026, 10, 17))['weekly_publication_limit'] == 16
    assert resolve_plan(config, date(2026, 10, 18))['weekly_publication_limit'] is None
    assert resolve_plan(config, date(2026, 10, 18))['target'] == 1
    assert resolve_plan(config, date(2026, 10, 19))['weekly_publication_limit'] == 5
    del config['strategy_reset']['transition_week']
    assert resolve_plan(config, date(2026, 10, 16))['weekly_publication_limit'] == 5


@pytest.mark.parametrize('field,value', [
    ('date', '2026-10-19'), ('category', 'origins'),
    ('weekly_publication_limit', False), ('weekly_publication_limit', 99),
    ('additional_manual_topics', ['줴줴이야', '티원']),
])
def test_owner_october_18_override_cannot_expand_scope(field, value):
    config = tomllib.loads((Path(__file__).parents[1] / 'config/blog.toml').read_text())
    config['strategy_reset']['owner_day_override'][field] = value
    with pytest.raises(ValueError, match='Invalid owner Oct 18'):
        resolve_plan(config, date(2026, 10, 18))


def test_removed_owner_override_restores_prior_sunday_hold():
    config = tomllib.loads((Path(__file__).parents[1] / 'config/blog.toml').read_text())
    del config['strategy_reset']['owner_day_override']
    plan = resolve_plan(config, date(2026, 10, 18))
    assert plan['target'] == 0
    assert plan['weekly_publication_limit'] == 16


def test_api_preparation_and_saver_never_invent_an_owner_input(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: pytest.fail('No source fetch'))
    monkeypatch.setattr('blogbot.pipeline.make_writer', lambda _: pytest.fail('No Naver write'))
    for result in [run_daily(settings), run_daily(settings, retry_failed=True), save_pending(settings)]:
        assert result[0]['status'] == 'AUTONOMOUS_WORK_REQUIRED'
    assert not settings.db_path.exists()


def test_cloud_prepare_reports_work_handoff_without_old_seeds_or_paid_recovery(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    monkeypatch.setattr('sys.argv', ['cloud', 'prepare'])
    monkeypatch.setattr(cloud, 'load_settings', lambda: settings)
    monkeypatch.setattr(cloud, 'restore', lambda _: None)
    monkeypatch.setattr(cloud, 'seed_inputs', lambda _: pytest.fail('No stale seed'))
    monkeypatch.setattr(cloud, 'run_daily', lambda *a, **k: pytest.fail('No paid preparer'))
    monkeypatch.setattr(cloud, 'pack', lambda *a: None)
    cloud.main()
    summary = json.loads((tmp_path / 'run-summary.json').read_text())
    assert summary['outcome'] == 'WAITING'
    assert summary['failed'] is summary['deliverable_ready'] is False


def prepare_publication(settings, request_id='current', *, saved=True):
    settings.artifact_dir.mkdir(parents=True, exist_ok=True)
    photo = settings.artifact_dir / 'generated-images' / request_id / 'photo.png'
    photo.parent.mkdir(parents=True, exist_ok=True)
    photo.write_bytes(b'offline image fixture')
    plan = settings.config['daily_plan']
    post = PostDraft('parenting', '육아생활', request_id, f'검증한 질문 {request_id}', '답입니다.',
                     [], ['https://example.org/source'], plan['date'], 28, 'APPROVED',
                     request_id=request_id, photos=[{'file': str(photo),
                     'sha256': hashlib.sha256(photo.read_bytes()).hexdigest()}],
                     provenance={'daily_plan': plan, 'source': 'work_autonomous',
                                 'strategy_version': plan['version']})
    with connect_db(settings.db_path) as conn:
        ident = save_post(conn, post)
        if saved:
            import_work_receipts(conn, {'verified_date': plan['date'], 'records': [{
                'request_id': request_id, 'day': plan['date'], 'category': 'parenting',
                'status': 'SAVED_NAVER'}]}, blog_id='test_owner', categories={'parenting'})
    review = {'scores': [5, 5, 4, 4, 5, 5], 'total': 28, 'decision': 'PASS',
              'issues': [], 'blocking_issues': [], 'rewrite_instructions': '',
              'source_checks': [{'status': 'SUPPORTED', 'claim': 'fixture',
                                 'source_url': 'https://example.org/source', 'evidence': 'fixture'}]}
    (settings.artifact_dir / f'{plan["date"]}-{ident:05d}.json').write_text(
        json.dumps({'post': asdict(post), 'input': {'photos': []}, 'review': review}))
    (settings.db_path.parent / 'ready.json').write_text(json.dumps({
        'date': plan['date'], 'daily_plan': plan, 'posts': [{'id': ident, 'post': asdict(post)}]}))


def publish_fixture(settings, request_id, number):
    day = settings.config['daily_plan']['date']
    with connect_db(settings.db_path) as conn:
        import_work_receipts(conn, {'verified_date': day, 'records': [{
            'request_id': request_id, 'day': day, 'category': 'parenting', 'status': 'PUBLISHED',
            'published_at': day + 'T12:00:00+09:00', 'post_id': str(number),
            'published_url': f'https://blog.naver.com/test_owner/{number}'}]},
            blog_id='test_owner', categories={'parenting'})


def scaled_settings(tmp_path, monkeypatch, *, actual_results=True):
    if actual_results:
        for i, day in enumerate(['2026-10-24', '2026-10-26', '2026-10-27', '2026-10-31', '2026-11-02']):
            settings = settings_at(tmp_path, monkeypatch, day)
            prepare_publication(settings, f'bootstrap-{i}')
            publish_fixture(settings, f'bootstrap-{i}', 100 + i)
    proof = {'verified_date': '2026-11-03', 'ledger_ref': 'offline-ledger:v1',
             'final_approval_ref': 'offline-final-approval:v1', 'next_ten_ready_ref': 'offline-ready:v1',
             'bootstrap_request_ids': [f'bootstrap-{i}' for i in range(5)],
             'bootstrap_elapsed_minutes': [40] * 5, 'bootstrap_browser_minutes': [20] * 5,
             'major_errors': 0,
             'next_ten_request_ids': [f'next-{i}' for i in range(10)],
             'next_ten_question_ids': [f'question-{i}' for i in range(10)]}
    text = Path(os.getenv('BLOG_CONFIG_PATH', Path(__file__).parents[1] / 'config/blog.toml')).read_text()
    text = text.replace('capacity_stage = "bootstrap5"', 'capacity_stage = "scaled10"')
    text = text.replace('weekly_publication_limit = 5', 'weekly_publication_limit = 10')
    text = text.replace('scale_evidence = {}', '')
    text += '\n[strategy_reset.scale_evidence]\n' + '\n'.join(
        f'{key} = {json.dumps(value)}' for key, value in proof.items())
    profile = tmp_path / 'scaled-blog.toml'
    profile.write_text(text)
    monkeypatch.setenv('BLOG_CONFIG_PATH', str(profile))
    return settings_at(tmp_path, monkeypatch, '2026-11-03')


def test_scaled_ten_requires_complete_observed_evidence():
    config = tomllib.loads((Path(__file__).parents[1] / 'config/blog.toml').read_text())
    config['strategy_reset'].update(capacity_stage='scaled10', weekly_publication_limit=10)
    with pytest.raises(ValueError, match='Scaled10 requires'):
        resolve_plan(config, date(2026, 11, 3))


@pytest.mark.parametrize('field,value', [
    ('major_errors', 1), ('bootstrap_browser_minutes', [50] * 5),
    ('next_ten_question_ids', ['same-question'] * 10), ('final_approval_ref', ''),
])
def test_scaled_ten_rejects_errors_overcapacity_duplicate_questions_and_missing_approval(
        tmp_path, monkeypatch, field, value):
    settings = scaled_settings(tmp_path, monkeypatch, actual_results=False)
    settings.config['strategy_reset']['scale_evidence'][field] = value
    with pytest.raises(ValueError, match='Scaled10 requires'):
        resolve_plan(settings.config)


def test_scaled_ten_has_two_daily_slots_and_preserves_three_week_topic_mix(tmp_path, monkeypatch):
    settings = scaled_settings(tmp_path, monkeypatch, actual_results=False)
    assert settings.daily_count == 2
    assert sum(c['max_daily'] for c in settings.config['categories'].values()) == 2
    counts = []
    for week in range(3):
        plans = [resolve_plan(settings.config, date(2026, 11, 16) + timedelta(days=week * 7 + i))
                 for i in range(7)]
        assert sum(p['target'] for p in plans) == 10
        counts.append(tuple(sum(p['target'] for p in plans if p['category'] == category)
                            for category in ('parenting', 'investment')))
    assert counts == [(6, 4), (8, 2), (6, 4)]


def test_transition_exception_returns_to_normal_scaled_ten_limit_on_october_19(tmp_path, monkeypatch):
    settings = scaled_settings(tmp_path, monkeypatch, actual_results=False)
    settings.config['strategy_reset']['scale_evidence']['verified_date'] = '2026-10-17'
    assert resolve_plan(settings.config, date(2026, 10, 17))['weekly_publication_limit'] == 16
    assert resolve_plan(settings.config, date(2026, 10, 19))['weekly_publication_limit'] == 10


def test_parallel_total_work_time_is_recorded_without_using_it_as_browser_capacity(tmp_path, monkeypatch):
    settings = scaled_settings(tmp_path, monkeypatch, actual_results=False)
    settings.config['strategy_reset']['scale_evidence']['bootstrap_elapsed_minutes'] = [500] * 5
    assert resolve_plan(settings.config)['target'] == 2


def test_scaled_ten_still_blocks_paid_api_and_accepts_explicit_work_input(tmp_path, monkeypatch):
    settings = scaled_settings(tmp_path, monkeypatch, actual_results=False)
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: pytest.fail('No source fetch'))
    monkeypatch.setattr('blogbot.pipeline.make_writer', lambda _: pytest.fail('No Naver write'))
    assert run_daily(settings)[0]['status'] == 'AUTONOMOUS_WORK_REQUIRED'
    assert save_pending(settings)[0]['status'] == 'AUTONOMOUS_WORK_REQUIRED'
    source = tmp_path / 'selected.json'
    source.write_text(json.dumps({'id': 'next-0', 'category': 'parenting',
        'question': '두 번째 독립 질문', 'source': 'work_autonomous', 'strategy_version': 'strategy-reset-v2'}))
    enqueue_file(settings, source)


@pytest.mark.usefixtures("archived_automatic_profile")
def test_archived_automatic_scaled_ten_cannot_publish_without_five_real_reviewed_results(tmp_path, monkeypatch):
    settings = scaled_settings(tmp_path, monkeypatch, actual_results=False)
    prepare_publication(settings)
    with connect_db(settings.db_path) as conn, pytest.raises(ValueError, match='five verified published'):
        claim_publication(conn, 'current', settings)


@pytest.mark.usefixtures("archived_automatic_profile")
def test_archived_automatic_historical_day_receipt_cannot_be_used_for_scaled_ten_bootstrap_proof(tmp_path, monkeypatch):
    settings = scaled_settings(tmp_path, monkeypatch)
    prepare_publication(settings)
    check = {'published_date': '2026-10-24', 'timezone': 'Asia/Seoul', 'published_post_checked': True,
             'checked_at': '2026-11-03T11:00:00+09:00', 'evidence_ref': 'offline-observation:bootstrap-0'}
    with connect_db(settings.db_path) as conn:
        conn.execute("UPDATE save_receipts SET published_at='', published_at_precision='day', "
                     "publication_check_json=? WHERE request_id='bootstrap-0'", (json.dumps(check),))
        conn.commit()
        with pytest.raises(ValueError, match='Scaled10 bootstrap evidence'):
            claim_publication(conn, 'current', settings)


@pytest.mark.usefixtures("archived_automatic_profile")
@pytest.mark.parametrize('day_only_count,allowed', [(3, True), (5, False)])
def test_archived_automatic_transition_counts_precise_and_day_publications_before_claim(tmp_path, monkeypatch, day_only_count, allowed):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-16')
    prepare_publication(settings, saved=False)
    records = []
    for i, day in enumerate(['2026-10-12'] * 2 + ['2026-10-13'] * 7 + ['2026-10-14'] * 2
                            + ['2026-10-15'] * day_only_count):
        records.append({'request_id': f'history-{i}', 'day': '2026-10-12', 'category': 'parenting',
                        'status': 'PUBLISHED', 'published_url': f'https://blog.naver.com/test_owner/{1000 + i}',
                        'post_id': str(1000 + i), 'published_at': day + 'T11:00:00+09:00',
                        'published_at_precision': 'minute'})
        if i >= 11:
            records[-1].update(published_at='', published_at_precision='day', publication_check={
                'published_date': day, 'timezone': 'Asia/Seoul', 'published_post_checked': True,
                'checked_at': '2026-10-16T08:30:00+09:00', 'evidence_ref': f'offline-list-observation:{i}'})
    with connect_db(settings.db_path) as conn:
        import_work_receipts(conn, {'verified_date': '2026-10-16', 'records': records},
                             blog_id='test_owner', categories={'parenting'})
        observed = conn.execute('SELECT * FROM save_receipts').fetchall()
        assert len(observed) == 11 + day_only_count
        assert sum(date(2026, 10, 15) in publication_days(row) for row in observed) == day_only_count
        assert all(any(date(2026, 10, 12) <= d <= date(2026, 10, 16) for d in publication_days(row))
                   for row in observed)
    cloud.filter_ready(tmp_path, {'verified_date': '2026-10-16', 'records': []},
                       settings.config['daily_plan'], settings)
    assert len(json.loads((tmp_path / 'ready.json').read_text())['posts']) == 1
    with connect_db(settings.db_path) as conn:
        import_work_receipts(conn, {'verified_date': '2026-10-16', 'records': [{
            'request_id': 'current', 'day': '2026-10-16', 'category': 'parenting', 'status': 'SAVED_NAVER'}]},
            blog_id='test_owner', categories={'parenting'})
    cloud.filter_ready(tmp_path, {'verified_date': '2026-10-16', 'records': []},
                       settings.config['daily_plan'], settings)
    assert len(json.loads((tmp_path / 'ready.json').read_text())['publication_ready']) == 1
    with connect_db(settings.db_path) as conn:
        if not allowed:
            with pytest.raises(ValueError, match='weekly publication limit'):
                claim_publication(conn, 'current', settings)
            return
        assert claim_publication(conn, 'current', settings)['status'] == 'PUBLICATION_CLAIMED'
    publish_fixture(settings, 'current', 2000)
    prepare_publication(settings, 'second')
    with connect_db(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM save_receipts WHERE status='PUBLISHED'").fetchone()[0] == 15
        with pytest.raises(ValueError, match='daily slot'):
            claim_publication(conn, 'second', settings)


@pytest.mark.usefixtures("archived_automatic_profile")
@pytest.mark.parametrize('used,allowed', [(9, True), (10, False)])
def test_archived_automatic_scaled_ten_weekly_cap_includes_bootstrap_publications(tmp_path, monkeypatch, used, allowed):
    settings = scaled_settings(tmp_path, monkeypatch)
    prepare_publication(settings)
    with connect_db(settings.db_path) as conn:
        # bootstrap-4 was published on Nov 2 and already consumes one slot this week.
        for i in range(used - 1):
            conn.execute('INSERT INTO save_receipts(request_id,day,category,status,published_at) '
                         'VALUES(?,?,?,?,?)', (f'other-{i}', '2026-11-02', 'exercise', 'PUBLISHED',
                                              '2026-11-02T12:00:00+09:00'))
        conn.commit()
        if allowed:
            assert claim_publication(conn, 'current', settings)['status'] == 'PUBLICATION_CLAIMED'
        else:
            with pytest.raises(ValueError, match='weekly publication limit'):
                claim_publication(conn, 'current', settings)


@pytest.mark.usefixtures("archived_automatic_profile")
@pytest.mark.parametrize('invalid', ['duplicate_publication', 'failed_final_review'])
def test_archived_automatic_scaled_ten_rechecks_actual_distinct_publications_and_final_reviews(tmp_path, monkeypatch, invalid):
    settings = scaled_settings(tmp_path, monkeypatch)
    prepare_publication(settings)
    with connect_db(settings.db_path) as conn:
        if invalid == 'duplicate_publication':
            conn.execute("UPDATE save_receipts SET post_id='100', published_url='https://m.blog.naver.com/test_owner/100' "
                         "WHERE request_id='bootstrap-1'")
            conn.commit()
        else:
            row = conn.execute("SELECT * FROM posts WHERE request_id='bootstrap-0'").fetchone()
            path = settings.artifact_dir / f'{row["as_of_date"]}-{row["id"]:05d}.json'
            packet = json.loads(path.read_text())
            packet['review']['decision'] = 'REWRITE'
            path.write_text(json.dumps(packet))
        with pytest.raises(ValueError, match='Scaled10'):
            claim_publication(conn, 'current', settings)


@pytest.mark.usefixtures("archived_automatic_profile")
def test_archived_automatic_scaled_ten_allows_second_handoff_and_claim_but_not_third(tmp_path, monkeypatch):
    settings = scaled_settings(tmp_path, monkeypatch)
    prepare_publication(settings, 'first')
    with connect_db(settings.db_path) as conn:
        assert claim_publication(conn, 'first', settings)['status'] == 'PUBLICATION_CLAIMED'
    publish_fixture(settings, 'first', 201)
    prepare_publication(settings, 'second', saved=False)
    monkeypatch.setenv('BLOG_BUNDLE_KEY', Fernet.generate_key().decode())
    cloud.pack(settings, tmp_path / 'handoff' / 'bundle.enc')
    ready_path = tmp_path / 'ready.json'
    assert [item['post']['request_id'] for item in json.loads(ready_path.read_text())['posts']] == ['second']
    cloud.filter_ready(tmp_path, {'verified_date': '2026-11-03', 'records': []},
                       settings.config['daily_plan'], settings)
    assert [item['post']['request_id'] for item in json.loads(ready_path.read_text())['posts']] == ['second']
    receipt = {'request_id': 'second', 'day': '2026-11-03', 'category': 'parenting', 'status': 'SAVED_NAVER'}
    with connect_db(settings.db_path) as conn:
        import_work_receipts(conn, {'verified_date': '2026-11-03', 'records': [receipt]},
                             blog_id='test_owner', categories={'parenting'})
    cloud.filter_ready(tmp_path, {'verified_date': '2026-11-03', 'records': [receipt]},
                       settings.config['daily_plan'], settings)
    assert [item['post']['request_id'] for item in json.loads(ready_path.read_text())['publication_ready']] == ['second']
    with connect_db(settings.db_path) as conn:
        assert claim_publication(conn, 'second', settings)['status'] == 'PUBLICATION_CLAIMED'
    publish_fixture(settings, 'second', 202)
    prepare_publication(settings, 'third')
    with connect_db(settings.db_path) as conn, pytest.raises(ValueError, match='daily slot'):
        claim_publication(conn, 'third', settings)


@pytest.mark.usefixtures("archived_automatic_profile")
def test_archived_automatic_scaled_ten_finishes_saved_first_post_before_second_input_or_handoff(tmp_path, monkeypatch):
    settings = scaled_settings(tmp_path, monkeypatch)
    prepare_publication(settings, 'first')
    first_ready = (tmp_path / 'ready.json').read_text()
    source = tmp_path / 'selected.json'
    source.write_text(json.dumps({'id': 'second', 'category': 'parenting',
        'question': '두 번째 독립 질문', 'source': 'work_autonomous', 'strategy_version': 'strategy-reset-v2'}))
    with pytest.raises(ValueError, match='Complete the saved draft publication'):
        enqueue_file(settings, source)
    prepare_publication(settings, 'second', saved=False)
    second_ready = (tmp_path / 'ready.json').read_text()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', Fernet.generate_key().decode())
    cloud.pack(settings, tmp_path / 'handoff' / 'bundle.enc')
    assert json.loads((tmp_path / 'ready.json').read_text())['posts'] == []
    (tmp_path / 'ready.json').write_text(second_ready)
    cloud.filter_ready(tmp_path, {'verified_date': '2026-11-03', 'records': []},
                       settings.config['daily_plan'], settings)
    assert json.loads((tmp_path / 'ready.json').read_text())['posts'] == []
    (tmp_path / 'ready.json').write_text(first_ready)
    cloud.filter_ready(tmp_path, {'verified_date': '2026-11-03', 'records': []},
                       settings.config['daily_plan'], settings)
    with connect_db(settings.db_path) as conn:
        assert claim_publication(conn, 'first', settings)['status'] == 'PUBLICATION_CLAIMED'


@pytest.mark.usefixtures("archived_automatic_profile")
@pytest.mark.parametrize('stage', ['bootstrap5', 'scaled10'])
def test_archived_automatic_bootstrap_evidence_survives_eight_day_bundle_restore(tmp_path, monkeypatch, stage):
    settings = scaled_settings(tmp_path, monkeypatch)
    settings = settings_at(tmp_path, monkeypatch, '2026-11-10')
    if stage == 'bootstrap5':
        policy = settings.config['strategy_reset']
        policy.update(capacity_stage='bootstrap5', weekly_publication_limit=5, scale_evidence={})
        settings.config['daily_plan'] = resolve_plan(settings.config)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    bundle = tmp_path / 'handoff' / 'bundle.enc'
    prepare_publication(settings, saved=False)
    cloud.pack(settings, bundle)
    target = tmp_path / 'restored'
    cloud.extract_bundle(bundle.read_bytes(), target, key)
    restored = replace(settings, db_path=target / 'blog.db', artifact_dir=target / 'drafts', inbox_dir=target / 'inbox')
    with connect_db(restored.db_path) as conn:
        for row in conn.execute("SELECT id,as_of_date FROM posts WHERE request_id LIKE 'bootstrap-%'"):
            assert (restored.artifact_dir / f'{row["as_of_date"]}-{row["id"]:05d}.json').is_file()
        import_work_receipts(conn, {'verified_date': '2026-11-10', 'records': [{
            'request_id': 'current', 'day': '2026-11-10', 'category': 'parenting', 'status': 'SAVED_NAVER'}]},
            blog_id='test_owner', categories={'parenting'})
    cloud.filter_ready(target, {'verified_date': '2026-11-10', 'records': []},
                       restored.config['daily_plan'], restored)
    with connect_db(restored.db_path) as conn:
        assert claim_publication(conn, 'current', restored)['status'] == 'PUBLICATION_CLAIMED'


@pytest.mark.usefixtures("archived_automatic_profile")
@pytest.mark.parametrize('used,allowed', [(4, True), (5, False)])
def test_archived_automatic_weekly_cap_counts_all_publications_before_claim(tmp_path, monkeypatch, used, allowed):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    prepare_publication(settings)
    with connect_db(settings.db_path) as conn:
        for i in range(used):
            day = f'2026-10-{19+i:02d}'
            conn.execute('INSERT INTO save_receipts(request_id,day,category,status,published_at) '
                         'VALUES(?,?,?,?,?)', (f'old-{i}', day, 'exercise', 'PUBLISHED', day+'T10:00:00+09:00'))
        conn.commit()
        if allowed:
            assert claim_publication(conn, 'current', settings)['status'] == 'PUBLICATION_CLAIMED'
        else:
            with pytest.raises(ValueError, match='weekly publication limit'):
                claim_publication(conn, 'current', settings)
            assert conn.execute("SELECT status FROM save_receipts WHERE request_id='current'").fetchone()[0] == 'SAVED_NAVER'


@pytest.mark.usefixtures("archived_automatic_profile")
@pytest.mark.parametrize('day', ['2026-10-16', '2026-10-17'])
@pytest.mark.parametrize('used,allowed', [(14, True), (15, True), (16, False)])
def test_archived_automatic_transition_claim_counts_every_prior_publication(tmp_path, monkeypatch, day, used, allowed):
    settings = settings_at(tmp_path, monkeypatch, day)
    prepare_publication(settings)
    with connect_db(settings.db_path) as conn:
        prior_days = ['12'] * 2 + ['13'] * 7 + ['14'] * 2 + ['15'] * (used - 11)
        for i, prior_day in enumerate(prior_days):
            stamp_day = f'2026-10-{prior_day}'
            conn.execute('INSERT INTO save_receipts(request_id,day,category,status,published_at) '
                         'VALUES(?,?,?,?,?)', (f'prior-{i}', stamp_day, 'exercise', 'PUBLISHED',
                                              stamp_day + 'T10:00:00+09:00'))
        conn.commit()
        if allowed:
            assert claim_publication(conn, 'current', settings)['status'] == 'PUBLICATION_CLAIMED'
        else:
            with pytest.raises(ValueError, match='weekly publication limit'):
                claim_publication(conn, 'current', settings)
            assert conn.execute("SELECT status FROM save_receipts WHERE request_id='current'").fetchone()[0] == 'SAVED_NAVER'


@pytest.mark.usefixtures("archived_automatic_profile")
@pytest.mark.parametrize('used', [18, 40])
def test_archived_automatic_october_18_owner_removal_allows_claim_without_erasing_history(tmp_path, monkeypatch, used):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-18')
    prepare_publication(settings)
    with connect_db(settings.db_path) as conn:
        for i in range(used):
            conn.execute('INSERT INTO save_receipts(request_id,day,category,status,published_at) '
                         'VALUES(?,?,?,?,?)', (f'prior-{i}', '2026-10-17', 'origins', 'PUBLISHED',
                                              '2026-10-17T10:00:00+09:00'))
        conn.commit()
        assert claim_publication(conn, 'current', settings)['status'] == 'PUBLICATION_CLAIMED'
        assert conn.execute("SELECT COUNT(*) FROM save_receipts WHERE status='PUBLISHED'").fetchone()[0] == used
        with pytest.raises(ValueError):
            claim_publication(conn, 'current', settings)


@pytest.mark.usefixtures("archived_automatic_profile")
def test_archived_automatic_transition_limit_still_rejects_a_second_publication_on_the_same_day(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-16')
    prepare_publication(settings, 'first')
    with connect_db(settings.db_path) as conn:
        assert claim_publication(conn, 'first', settings)['status'] == 'PUBLICATION_CLAIMED'
    publish_fixture(settings, 'first', 501)
    prepare_publication(settings, 'second')
    with connect_db(settings.db_path) as conn, pytest.raises(ValueError, match='daily slot'):
        claim_publication(conn, 'second', settings)


@pytest.mark.usefixtures("archived_automatic_profile")
@pytest.mark.parametrize('day', ['2026-10-15', '2026-10-22', '2026-10-25'])
def test_archived_automatic_nonpublication_day_rejects_before_opening_a_handoff(tmp_path, monkeypatch, day):
    settings = settings_at(tmp_path, monkeypatch, day)
    with (connect_db(settings.db_path) as conn,
          pytest.raises(ValueError, match='no new-publication slot')):
        claim_publication(conn, 'not-created', settings)


@pytest.mark.usefixtures("archived_automatic_profile")
def test_archived_automatic_daily_slot_remains_one_after_weekly_limit_increases(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    prepare_publication(settings)
    with connect_db(settings.db_path) as conn:
        conn.execute('INSERT INTO save_receipts(request_id,day,category,status) VALUES(?,?,?,?)',
                     ('other', '2026-10-24', 'parenting', 'SAVED_NAVER'))
        conn.commit()
        with pytest.raises(ValueError, match='Another delivery already occupies the daily slot'):
            claim_publication(conn, 'current', settings)
        assert conn.execute("SELECT status FROM save_receipts WHERE request_id='current'").fetchone()[0] == 'SAVED_NAVER'


def test_autonomous_input_is_labeled_without_fabricating_an_owner_question(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    source = tmp_path / 'selected.json'
    source.write_text(json.dumps({'id': 'work-selected', 'category': 'parenting',
        'question': '자료로 검증할 육아 질문', 'source': 'work_autonomous',
        'strategy_version': 'strategy-reset-v2'}))
    enqueue_file(settings, source)
    packet = json.loads((settings.inbox_dir / 'work-selected/request.json').read_text())
    assert packet['provenance']['source'] == 'work_autonomous'
    settings = settings_at(tmp_path, monkeypatch, '2026-10-25')
    with pytest.raises(ValueError, match='current approved Work slot'):
        enqueue_file(settings, source)


@pytest.mark.parametrize('version', ['strategy-reset-v1', 'strategy-reset-v2'])
def test_financial_source_checks_still_apply_to_autonomous_selection(tmp_path, monkeypatch, version):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-28')
    source = {'url': 'https://www.nts.go.kr/nts/doc/guide', 'published_date': '2026-10-01',
              'excerpt': '검증할 공식 조건입니다.', 'date_excerpt': '2026-10-01'}
    request = ContentRequest('work-money', 'investment', {
        'investment_mode': 'life-economics-v1', 'question': '검증할 세금 질문',
        'benchmark_query': '연금 납입 조건', 'sources': [source]}, provenance={
        'source': 'work_autonomous', 'strategy_version': version})
    assert request_contract(request)
    plan = {**settings.config['daily_plan'], 'version': version}
    post = PostDraft('investment', '생활경제', 'question', '질문', '조건을 확인합니다.',
        [], [source['url']], plan['date'], provenance={**request.provenance,
        'life_economics_checks': [{**source, 'verified_date': plan['date'], 'sha256': 'a' * 64}]})
    assert post_contract(post, plan)
    other_version = 'strategy-reset-v2' if version == 'strategy-reset-v1' else 'strategy-reset-v1'
    assert not post_contract(post, {**plan, 'version': other_version})
    assert not post_contract(post, {**plan, 'version': 'weekly-2221-v1', 'work_managed': False})
    post.provenance['life_economics_checks'] = []
    assert not post_contract(post, plan)


@pytest.mark.parametrize('stage', ['bootstrap5', 'scaled10'])
@pytest.mark.parametrize('status', ['SAVED_NAVER', 'APPROVED', 'PUBLISH_UNCERTAIN'])
def test_owner_only_rejects_claim_without_any_database_or_handoff_change(
        tmp_path, monkeypatch, stage, status):
    settings = (scaled_settings(tmp_path, monkeypatch) if stage == 'scaled10'
                else settings_at(tmp_path, monkeypatch, '2026-10-24'))
    prepare_publication(settings)
    with connect_db(settings.db_path) as conn:
        conn.execute("UPDATE posts SET status=? WHERE request_id='current'", (status,))
        conn.commit()
        before = '\n'.join(conn.iterdump())
        handoff = (tmp_path / 'ready.json').read_bytes()
        for _ in range(2):
            with pytest.raises(ValueError, match='Owner-only publication'):
                claim_publication(conn, 'current', settings)
        assert '\n'.join(conn.iterdump()) == before
        assert (tmp_path / 'ready.json').read_bytes() == handoff


def test_owner_only_cli_rejects_before_database_access(tmp_path, monkeypatch, capsys):
    from blogbot import cli
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    monkeypatch.setattr(cli, 'load_settings', lambda: settings)
    monkeypatch.setattr(cli, 'connect_db', lambda *a: pytest.fail('No database write or event'))
    monkeypatch.setattr('sys.argv', ['blogbot', 'claim-publication', '--request-id', 'current'])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out) == {
        'status': 'PUBLICATION_DISABLED', 'reason': 'owner_only'}
    assert not settings.db_path.exists()


@pytest.mark.parametrize('mode', [None, 'owner_only', 'unknown'])
def test_missing_or_unknown_publication_mode_fails_closed(tmp_path, monkeypatch, mode):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    settings.config['blog']['publication_mode'] = mode
    with connect_db(settings.db_path) as conn:
        with pytest.raises(ValueError, match='Owner-only publication'):
            claim_publication(conn, 'missing', settings)
        assert conn.execute('SELECT COUNT(*) FROM save_receipts').fetchone()[0] == 0


def test_owner_only_drops_stale_publication_signals_and_keeps_manual_observations(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    prepare_publication(settings)
    path = tmp_path / 'ready.json'
    ready = json.loads(path.read_text())
    ready['publication_ready'] = ready['posts']
    ready['posts'] = []
    path.write_text(json.dumps(ready))
    cloud.filter_ready(tmp_path, {'verified_date': '2026-10-24', 'records': []},
                       settings.config['daily_plan'], settings)
    filtered = json.loads(path.read_text())
    assert filtered['posts'] == filtered['publication_ready'] == []
    publish_fixture(settings, 'current', 12345)
    publish_fixture(settings, 'current', 12345)  # Observation import remains idempotent.
    with connect_db(settings.db_path) as conn:
        row = dict(conn.execute("SELECT * FROM save_receipts WHERE request_id='current'").fetchone())
        assert row['status'] == 'PUBLISHED'
        assert row['publish_attempted_at'] == ''
        assert row['published_url'] == 'https://blog.naver.com/test_owner/12345'
        assert publication_days(row) == {date(2026, 10, 24)}


def test_owner_only_scaled_ten_allows_second_draft_before_first_owner_publication(tmp_path, monkeypatch):
    settings = scaled_settings(tmp_path, monkeypatch)
    prepare_publication(settings, 'first')
    source = tmp_path / 'second.json'
    source.write_text(json.dumps({'id': 'second', 'category': 'parenting',
        'question': '두 번째 독립 질문', 'source': 'work_autonomous', 'strategy_version': 'strategy-reset-v2'}))
    enqueue_file(settings, source)
    with pytest.raises(FileExistsError):
        enqueue_file(settings, source)
    prepare_publication(settings, 'second', saved=False)
    monkeypatch.setenv('BLOG_BUNDLE_KEY', Fernet.generate_key().decode())
    cloud.pack(settings, tmp_path / 'handoff' / 'bundle.enc')
    cloud.filter_ready(tmp_path, {'verified_date': '2026-11-03', 'records': []},
                       settings.config['daily_plan'], settings)
    ready = json.loads((tmp_path / 'ready.json').read_text())
    assert [item['post']['request_id'] for item in ready['posts']] == ['second']
    assert ready['publication_ready'] == []
    # Uncertain saves continue to block the next editing handoff.
    with connect_db(settings.db_path) as conn:
        conn.execute("UPDATE save_receipts SET status='SAVE_UNCERTAIN' WHERE request_id='first'")
        conn.commit()
    cloud.filter_ready(tmp_path, {'verified_date': '2026-11-03', 'records': []},
                       settings.config['daily_plan'], settings)
    assert json.loads((tmp_path / 'ready.json').read_text())['posts'] == []


@pytest.mark.parametrize('day,ids', [
    ('2026-10-19', ['owner-20261019-sunwoo-moon-ai', 'owner-20261019-cxmt-semiconductor-analysis']),
    ('2026-10-20', ['owner-20261020-sunwoo-telekinesis-ai', 'owner-20261020-sunwoo-giant-teddy-ai']),
])
def test_dated_owner_drafts_replace_regular_generation_without_duplicate_work(tmp_path, monkeypatch, day, ids):
    settings = settings_at(tmp_path, monkeypatch, day)
    plan = settings.config['daily_plan']
    assert plan['topic_selection'] == 'owner_fixed'
    assert plan['reservation']['request_ids'] == ids
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda *a: pytest.fail('No new topic'))
    monkeypatch.setattr('blogbot.pipeline.make_writer', lambda *a: pytest.fail('No native edit'))
    for result in (run_daily(settings), run_daily(settings, retry_failed=True), save_pending(settings)):
        assert result[0]['status'] == 'EDITORIAL_SLOT_RESERVED'
        assert result[0]['request_ids'] == ids
    source = tmp_path / 'duplicate.json'
    source.write_text(json.dumps({'id': 'duplicate', 'category': 'parenting', 'question': '새 질문',
        'source': 'work_autonomous', 'strategy_version': 'strategy-reset-v2'}))
    with pytest.raises(ValueError, match='current approved Work slot'):
        enqueue_file(settings, source)
    assert not settings.db_path.exists()
