"""Focused execution, no duplicate API work, and real publication caps."""
import hashlib
import json
import tomllib
from dataclasses import asdict
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from blogbot import cloud
from blogbot.config import load_settings
from blogbot.core import PostDraft, claim_publication, connect_db, import_work_receipts, save_post
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
    ('2026-10-16', 'audit', 0, 'parenting'),
    ('2026-10-17', 'pilot', 0, 'parenting'),
    ('2026-10-21', 'pilot', 0, 'investment'),
    ('2026-10-22', 'focused', 0, 'parenting'),
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
    assert sum(p['target'] for p in plans) == 3
    assert [p['category'] for p in plans if p['target']] == ['parenting', 'investment', 'parenting']
    assert resolve_plan(config, date(2026, 11, 12))['review_due'] is True
    assert resolve_plan(config, date(2026, 11, 12))['version'] == 'strategy-reset-v1'


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


def prepare_publication(settings):
    settings.artifact_dir.mkdir(parents=True)
    photo = settings.artifact_dir / 'photo.png'
    photo.write_bytes(b'offline image fixture')
    plan = settings.config['daily_plan']
    post = PostDraft('parenting', '육아생활', 'question', '검증한 질문', '답입니다.',
                     [], ['https://example.org/source'], plan['date'], 28, 'APPROVED',
                     request_id='current', photos=[{'file': str(photo),
                     'sha256': hashlib.sha256(photo.read_bytes()).hexdigest()}],
                     provenance={'daily_plan': plan, 'source': 'work_autonomous'})
    with connect_db(settings.db_path) as conn:
        ident = save_post(conn, post)
        import_work_receipts(conn, {'verified_date': plan['date'], 'records': [{
            'request_id': 'current', 'day': plan['date'], 'category': 'parenting',
            'status': 'SAVED_NAVER'}]}, blog_id='test_owner', categories={'parenting'})
    review = {'scores': [5, 5, 4, 4, 5, 5], 'total': 28, 'decision': 'PASS',
              'issues': [], 'blocking_issues': [], 'rewrite_instructions': '',
              'source_checks': [{'status': 'SUPPORTED', 'claim': 'fixture',
                                 'source_url': 'https://example.org/source', 'evidence': 'fixture'}]}
    (settings.artifact_dir / f'{plan["date"]}-{ident:05d}.json').write_text(
        json.dumps({'post': asdict(post), 'input': {'photos': []}, 'review': review}))
    (settings.db_path.parent / 'ready.json').write_text(json.dumps({
        'date': plan['date'], 'daily_plan': plan, 'posts': [{'id': ident, 'post': asdict(post)}]}))


@pytest.mark.parametrize('used,allowed', [(2, True), (3, False)])
def test_weekly_cap_counts_all_publications_before_claim(tmp_path, monkeypatch, used, allowed):
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


def test_nonpublication_day_rejects_before_opening_a_handoff(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-15')
    with (connect_db(settings.db_path) as conn,
          pytest.raises(ValueError, match='no new-publication slot')):
        claim_publication(conn, 'not-created', settings)


def test_autonomous_input_is_labeled_without_fabricating_an_owner_question(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-24')
    source = tmp_path / 'selected.json'
    source.write_text(json.dumps({'id': 'work-selected', 'category': 'parenting',
        'question': '자료로 검증할 육아 질문', 'source': 'work_autonomous',
        'strategy_version': 'strategy-reset-v1'}))
    enqueue_file(settings, source)
    packet = json.loads((settings.inbox_dir / 'work-selected/request.json').read_text())
    assert packet['provenance']['source'] == 'work_autonomous'
    settings = settings_at(tmp_path, monkeypatch, '2026-10-25')
    with pytest.raises(ValueError, match='current approved Work slot'):
        enqueue_file(settings, source)


def test_financial_source_checks_still_apply_to_autonomous_selection(tmp_path, monkeypatch):
    settings = settings_at(tmp_path, monkeypatch, '2026-10-28')
    source = {'url': 'https://www.nts.go.kr/nts/doc/guide', 'published_date': '2026-10-01',
              'excerpt': '검증할 공식 조건입니다.', 'date_excerpt': '2026-10-01'}
    request = ContentRequest('work-money', 'investment', {
        'investment_mode': 'life-economics-v1', 'question': '검증할 세금 질문',
        'benchmark_query': '연금 납입 조건', 'sources': [source]}, provenance={
        'source': 'work_autonomous', 'strategy_version': 'strategy-reset-v1'})
    assert request_contract(request)
    plan = settings.config['daily_plan']
    post = PostDraft('investment', '생활경제', 'question', '질문', '조건을 확인합니다.',
        [], [source['url']], plan['date'], provenance={**request.provenance,
        'life_economics_checks': [{**source, 'verified_date': plan['date'], 'sha256': 'a' * 64}]})
    assert post_contract(post, plan)
    assert not post_contract(post, {**plan, 'version': 'weekly-2221-v1', 'work_managed': False})
    post.provenance['life_economics_checks'] = []
    assert not post_contract(post, plan)
