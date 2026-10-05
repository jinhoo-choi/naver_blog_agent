"""Offline plan boundaries. No paid calls, external schedules, or real Naver saves."""
import json
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import UTC, date, datetime, timedelta
from threading import Barrier

import pytest
from cryptography.fernet import Fernet

from blogbot import cloud
from blogbot.config import load_settings
from blogbot.core import PostDraft, connect_db, reserve_attempt, save_post, today_kst
from blogbot.editorial import TOPICS, quality_guidance
from blogbot.images import atomic_json, image_prompt
from blogbot.inputs import ContentRequest, collect_requests, enqueue_file
from blogbot.pipeline import run_daily, save_pending
from blogbot.planning import active_plan, ready_categories, resolve_plan, saved_count


def set_clock(monkeypatch, stamp):
    class Fixed(datetime):
        @classmethod
        def now(cls, tz=None):
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)
    monkeypatch.setattr('blogbot.core.datetime', Fixed)


def settings_on(tmp_path, monkeypatch, day='2026-10-05', *, keep_reservation=False):
    set_clock(monkeypatch, datetime.fromisoformat(day + 'T03:00:00+00:00'))
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('BLOG_DAILY_COUNT', '3')  # A stale external default cannot expand the plan.
    settings = load_settings()
    if not keep_reservation:
        # Isolate generic daily guards from the separately tested owner-selected Oct 5 exception.
        settings.config['operating_plan'].pop('reservations', None)
        settings.config['daily_plan'] = resolve_plan(settings.config)
    settings.artifact_dir.mkdir(parents=True)
    return settings


def plan_draft(settings, identity='question', *, status='APPROVED', age=0, category=None):
    plan = active_plan(settings)
    return PostDraft(category or plan['category'], '육아생활', identity, identity,
                     '답변입니다.\n\n## 확인\n\n설명합니다.', [], ['https://example.org/source'],
                     str(today_kst() - timedelta(days=age)), 27, status,
                     request_id=identity, provenance={'daily_plan': plan})


def write_packet(settings, post):
    request = ContentRequest(post.request_id, post.category,
                             {'question': '확인할 실제 질문', 'editorial_type':
                              post.provenance.get('editorial_type', 'article')},
                             provenance=post.provenance)
    with connect_db(settings.db_path) as conn:
        ident = save_post(conn, post)
    atomic_json(settings.artifact_dir / f'{post.as_of_date}-{ident:05d}.json',
                {'post': asdict(post), 'input': asdict(request), 'review': {'total': 27}})
    return ident


@pytest.mark.parametrize('offset', range(14))
def test_every_measurement_day_has_one_explicit_slot(tmp_path, monkeypatch, offset):
    day = date(2026, 10, 5) + timedelta(days=offset)
    settings = settings_on(tmp_path, monkeypatch, str(day))
    plan = active_plan(settings)
    expected = ['parenting', 'origins', 'parenting', 'exercise', 'investment',
                'parenting', 'parenting'][day.weekday()]
    assert plan['date'] == str(day) and plan['category'] == expected and plan['target'] == 1
    assert plan['editorial_types'] == (['article', 'ai_tutorial'] if expected == 'parenting' else ['article'])
    assert settings.daily_count == settings.config['blog']['daily_max'] == 1
    assert sum(info['max_daily'] for info in settings.config['categories'].values()) == 1
    assert settings.config['categories'][expected]['max_daily'] == 1
    assert settings.config['community']['enabled'] == (expected == 'investment')
    if expected == 'origins':
        assert plan['depth'] == 'short'
        assert not any('실제 캡처/직접 제공 사진' in r for r in settings.config['categories'][expected]['rules'])
    else:
        assert any('실제 캡처' in r for r in settings.config['categories'][expected]['rules'])
    assert [settings.config['images'][c + '_count'] for c in
            ['parenting', 'exercise', 'investment']] == [5, 5, 3]
    assert settings.config['images']['max_attempts'] == 2
    assert settings.config['blog']['review_pass_score'] == 24


def test_two_week_totals_and_no_automatic_expiry(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    start = date(2026, 10, 5)
    assert Counter(resolve_plan(settings.config, start + timedelta(days=i))['category']
                   for i in range(14)) == {'parenting': 8, 'origins': 2, 'exercise': 2, 'investment': 2}
    for day in ['2026-10-19', '2026-11-02']:
        assert resolve_plan(settings.config, date.fromisoformat(day))['target'] == 1


@pytest.mark.parametrize('stamp,expected', [
    ('2026-10-04T14:59:59+00:00', None),
    ('2026-10-04T15:00:00+00:00', '2026-10-05'),
    ('2026-10-18T14:59:59+00:00', '2026-10-18'),
    ('2026-10-18T15:00:00+00:00', '2026-10-19'),
])
def test_kst_start_and_review_window_boundary(tmp_path, monkeypatch, stamp, expected):
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    set_clock(monkeypatch, datetime.fromisoformat(stamp))
    plan = active_plan(load_settings())
    assert (plan['date'] if plan else None) == expected


def test_long_running_process_must_reload_plan_at_kst_midnight(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    set_clock(monkeypatch, datetime(2026, 10, 5, 15, tzinfo=UTC))
    with pytest.raises(ValueError, match='Reload'):
        run_daily(settings)


def test_family_ai_queue_requires_explicit_subtype_and_does_not_fetch_kis(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-07')
    monkeypatch.setattr('blogbot.inputs.fetch_community', lambda _: pytest.fail('Unscheduled KIS read'))
    source = tmp_path / 'input.json'
    for identity, category, subtype in [('p', 'parenting', None),
                                        ('a', 'parenting', 'ai_tutorial'),
                                        ('e', 'exercise', None)]:
        raw = {'id': identity, 'category': category, 'question': '사용자가 입력한 질문입니다'}
        if subtype:
            raw['editorial_type'] = subtype
        source.write_text(json.dumps(raw))
        enqueue_file(settings, source)
    requests, notices = collect_requests(settings)
    assert [r.id for r in requests] == ['a', 'p'] and not notices
    assert requests[0].category == 'parenting'
    assert requests[0].data['editorial_type'] == 'ai_tutorial'
    assert settings.config['categories']['parenting']['naver_category_no'] == 1
    for bad in [{'category': 'exercise', 'editorial_type': 'ai_tutorial'},
                {'category': 'parenting', 'editorial_type': 'invented'}]:
        source.write_text(json.dumps({'id': 'bad', 'question': '실제 질문', **bad}))
        with pytest.raises(ValueError, match='subtype'):
            enqueue_file(settings, source)


def test_missing_planned_input_holds_without_paid_calls_or_filler(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-07')
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', lambda *a: pytest.fail('No paid filler'))
    monkeypatch.setattr('blogbot.inputs.fetch_community', lambda _: pytest.fail('No KIS read'))
    results = run_daily(settings, count=3)
    assert results == [{'status': 'PLANNED_INPUT_REQUIRED', 'category': 'parenting',
                        'editorial_types': ['article', 'ai_tutorial']}]
    assert not list(settings.inbox_dir.glob('*/request.json'))
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0


@pytest.mark.parametrize('snapshot,source_day,status', [
    ('2026-10-08T09:00:00+09:00', '2026-10-09', 'COMMUNITY_SOURCE_PENDING'),
    ('2026-10-09T06:59:59+09:00', '2026-10-09', 'COMMUNITY_SOURCE_PENDING'),
    ('2026-10-09T08:00:00+09:00', '2026-10-07', 'NO_ELIGIBLE_INVESTMENT'),
    ('2026-10-09T07:00:00+09:00', '2026-10-08', None),
    ('2026-10-09T07:00:00+09:00', '2026-10-09', None),
])
def test_friday_keeps_source_and_snapshot_freshness(tmp_path, monkeypatch, snapshot, source_day, status):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-09')
    record = {'id': 'source', 'kind': 'policy', 'facts': f'자료 기준일 {source_day}',
              'src': 'https://example.org/source', 'body': '공식 자료의 요약',
              'score': {'factual': 5, 'useful': 4, 'natural': 4, 'compliant': 5,
                        'gain': 4, 'fit': 4, 'fatal': []}}
    origin = {'repository': 'owner/source', 'snapshot_date': snapshot[:10],
              'snapshot_at': snapshot, 'commit': 'a' * 40}
    monkeypatch.setattr('blogbot.inputs.fetch_community', lambda _: ([record], origin))
    requests, notices = collect_requests(settings)
    assert ([n['status'] for n in notices] == [status]) if status else not notices
    assert len(requests) == int(status is None)
    if requests:
        assert requests[0].category == 'investment'


def test_attempt_cap_survives_replay_and_only_existing_bounded_replacement(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    plan = active_plan(settings)
    candidates = [ContentRequest(f'q{i}', 'parenting', {'question': '실제 질문'}) for i in range(3)]
    with connect_db(settings.db_path) as conn:
        first = reserve_attempt(conn, settings.config, 3, candidates)
        assert first and reserve_attempt(conn, settings.config, 3, candidates) is None
        conn.execute("UPDATE attempts SET status='DROP_REVIEW' WHERE id=?", (first[0],))
        conn.commit()
    with connect_db(settings.db_path) as conn:
        assert reserve_attempt(conn, settings.config, 3, candidates)
        assert reserve_attempt(conn, settings.config, 3, candidates) is None
        assert [json.loads(r[0]) for r in conn.execute('SELECT plan_json FROM attempts')] == [plan, plan]


def test_only_one_same_day_media_resume_many_carryovers_preserved(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    for identity, age in [('old1', 1), ('old2', 2), ('today1', 0), ('today2', 0)]:
        write_packet(settings, plan_draft(settings, identity, status='IMAGES_PENDING', age=age))
    calls = []
    monkeypatch.setattr('blogbot.pipeline.generate_images',
                        lambda s, r, p: calls.append(p.request_id) or p)
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', lambda *a: pytest.fail('No new writer'))
    run_daily(settings, count=3)
    run_daily(settings, count=3, retry_failed=True)
    assert calls == ['today1']
    with connect_db(settings.db_path) as conn:
        assert [(r['request_id'], r['status']) for r in conn.execute('SELECT * FROM posts')] == [
            ('old1', 'IMAGES_PENDING'), ('old2', 'IMAGES_PENDING'),
            ('today1', 'APPROVED'), ('today2', 'IMAGES_PENDING')]
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0


def test_legacy_attempt_cannot_buy_new_work_under_changed_profile(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    source = tmp_path / 'q.json'
    source.write_text(json.dumps({'id': 'legacy', 'category': 'parenting', 'question': '실제 질문'}))
    enqueue_file(settings, source)
    with connect_db(settings.db_path) as conn:
        conn.execute("INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,'ERROR')",
                     (str(today_kst()), 'parenting', 'legacy'))
        conn.commit()
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', lambda *a: pytest.fail('No new paid recovery'))
    run_daily(settings, retry_failed=True)
    with connect_db(settings.db_path) as conn:
        assert tuple(conn.execute('SELECT status,plan_json FROM attempts').fetchone()) == ('ERROR', '{}')


def test_handoff_exports_one_current_plan_item_and_requires_dated_ledger(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    for identity, age, category in [('old', 1, None), ('exercise', 0, 'exercise'),
                                    ('new1', 0, None), ('new2', 0, None)]:
        write_packet(settings, plan_draft(settings, identity, age=age, category=category))
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    cloud.pack(settings, tmp_path / 'bundle.enc')
    ready_path = settings.db_path.parent / 'ready.json'
    original = json.loads(ready_path.read_text())
    plan = active_plan(settings)
    assert original['daily_plan'] == plan
    assert [r['post']['request_id'] for r in original['posts']] == ['new1']
    receipts = {'verified_date': str(today_kst()), 'records': []}
    cloud.filter_ready(settings.db_path.parent, receipts, plan)
    assert len(json.loads(ready_path.read_text())['posts']) == 1
    receipts['records'] = [{'request_id': 'unrelated-save', 'status': 'SAVED_NAVER',
                            'day': str(today_kst())}]
    cloud.filter_ready(settings.db_path.parent, receipts, plan)
    assert json.loads(ready_path.read_text())['posts'] == []
    atomic_json(ready_path, original)
    receipts['records'][0].pop('day')
    with pytest.raises(ValueError, match='save day'):
        cloud.filter_ready(settings.db_path.parent, receipts, plan)
    with pytest.raises(ValueError, match='daily plan'):
        cloud.filter_ready(settings.db_path.parent, {'verified_date': str(today_kst()), 'records': []})
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT COUNT(*) FROM posts').fetchone()[0] == 4


def test_saver_one_total_across_replay_and_parallel_runs(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    for identity in ['new1', 'new2']:
        write_packet(settings, plan_draft(settings, identity))
    barrier, saves = Barrier(2), []
    class Writer:
        def preflight(self, post):
            barrier.wait(timeout=5)
        def save(self, post):
            saves.append(post.request_id)
    monkeypatch.setattr('blogbot.pipeline.make_writer', lambda _: Writer())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: save_pending(settings), range(2)))
    assert sum(r.get('status') == 'SAVED_NAVER' for run in results for r in run) == 1
    assert len(saves) == 1 and not save_pending(settings)
    with connect_db(settings.db_path) as conn:
        assert saved_count(conn, active_plan(settings)) == 1
        assert ready_categories(settings, conn) == {'parenting'}


def test_old_manuscript_saved_today_consumes_today_slot(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    ident = write_packet(settings, plan_draft(settings, 'older', status='SAVED_NAVER', age=2))
    with connect_db(settings.db_path) as conn:
        conn.execute('UPDATE posts SET draft_saved_at=? WHERE id=?',
                     ('2026-10-04T16:00:00+00:00', ident))
        conn.commit()
        assert saved_count(conn, active_plan(settings)) == 1
        assert ready_categories(settings, conn) == set()  # Volume cap is not successful plan completion.
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: pytest.fail('Already consumed'))
    assert run_daily(settings)[0]['status'] == 'DAILY_PLAN_LIMIT'


def test_ai_subtype_all_four_llm_paths_and_images_keep_budgets(tmp_path, monkeypatch):
    from blogbot import llm
    from blogbot.llm import BlogLLM
    from blogbot.pre_review import DraftCandidate
    captured = []
    source = 'https://example.org/source'
    payload = {'title': '제목', 'subcategory': '육아생활', 'body': '설명입니다.', 'tags': [],
               'source_urls': [source], 'scores': [4] * 6, 'total': 24, 'decision': 'PASS',
               'issues': [], 'blocking_issues': [], 'rewrite_instructions': '',
               'source_checks': [{'claim': '사실', 'source_url': source, 'evidence': '근거',
                                  'status': 'SUPPORTED'}]}
    response = {'output': [{'status': 'completed', 'action': {'type': 'open_page', 'url': source}}]}
    monkeypatch.setattr(llm, 'request_json', lambda *a, **kw: captured.append(kw) or (dict(payload), response))
    monkeypatch.setattr('blogbot.research.prepare_reference_evidence', lambda d, r, u: r)
    client = BlogLLM.__new__(BlogLLM)
    client.client, client.journal = None, None
    client.model = client.review_model = 'offline'
    client.writer_prompt = client.reviewer_prompt = '공통 프롬프트'
    request = ContentRequest('ai', 'parenting', {'question': '가족 사진 정리 방법',
                                              'editorial_type': 'ai_tutorial'})
    info = {'display_name': '육아', 'subcategories': ['육아생활']}
    post = client.create_draft(request, info, [])
    review = client.review(post, info, request)
    client.rewrite(post, info, review, request)
    client.correct_draft(DraftCandidate(dict(payload), [source]), info, ['invalid_field_tags'], request)
    assert len(captured) == 4
    for call in captured:
        assert TOPICS['ai_tutorial'] in call['input'] and TOPICS['parenting'] not in call['input']
    assert 'tools' not in captured[-1] and captured[-1]['max_output_tokens'] == 12000
    assert all(c['max_tool_calls'] == 3 for c in captured[:3])
    prompt = image_prompt(post, '확인한 설정', editorial_type='ai_tutorial')
    assert 'actual screenshots' in prompt and 'Never fabricate application screens' in prompt
    assert 'one plainly recognizable awake baby' not in prompt
    assert quality_guidance('parenting') == quality_guidance('parenting', None)


def test_imported_receipt_preserves_attempt_day_and_counts_actual_save_day(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    settings = replace(settings, root=tmp_path / 'repo')
    (settings.root / 'config').mkdir(parents=True)
    with connect_db(settings.db_path) as conn:
        conn.execute("INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,'ERROR')",
                     ('2026-10-04', 'parenting', 'old'))
        conn.commit()
    receipt = {'records': [{'request_id': 'old', 'category': 'parenting', 'status': 'SAVED_NAVER',
                            'saved_display': '2026.10.05 09:16', 'saved_display_timezone': 'Asia/Seoul'}]}
    (settings.root / 'config/manual-saves.json').write_text(json.dumps(receipt))
    cloud.import_manual_saves(settings)
    cloud.import_manual_saves(settings)
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT day FROM attempts').fetchone()[0] == '2026-10-04'
        assert conn.execute('SELECT day FROM save_receipts').fetchone()[0] == '2026-10-05'
        assert saved_count(conn, active_plan(settings)) == 1
        assert ready_categories(settings, conn) == set()
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: pytest.fail('Already saved'))
    assert run_daily(settings)[0]['status'] == 'DAILY_PLAN_LIMIT'


def test_current_receipt_completes_matching_api_post_still_approved(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    write_packet(settings, plan_draft(settings, 'current'))
    settings = replace(settings, root=tmp_path / 'repo')
    (settings.root / 'config').mkdir(parents=True)
    (settings.root / 'config/manual-saves.json').write_text(json.dumps({'records': [
        {'request_id': 'current', 'category': 'parenting', 'status': 'SAVED_NAVER',
         'day': '2026-10-05', 'saved_at': '2026-10-05T00:16:00+00:00'}]}))
    cloud.import_manual_saves(settings)
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT status FROM posts').fetchone()[0] == 'APPROVED'
        assert ready_categories(settings, conn) == {'parenting'}
    monkeypatch.setattr('sys.argv', ['cloud', 'prepare'])
    monkeypatch.setattr(cloud, 'load_settings', lambda: settings)
    monkeypatch.setattr(cloud, 'restore', lambda _: None)
    monkeypatch.setattr(cloud, 'seed_inputs', lambda _: None)
    monkeypatch.setattr(cloud, 'pack', lambda *a: None)
    cloud.main()
    assert json.loads((tmp_path / 'run-summary.json').read_text())['failed'] is False


@pytest.mark.parametrize('record,expected', [
    ({'day': '2026-10-05'}, '2026-10-05'),
    ({'saved_display': '2026.10.05 09:15'}, None),
    ({'saved_display': '2026.10.05 09:15', 'saved_display_timezone': 'Asia/Seoul'}, '2026-10-05'),
    ({'draft_saved_at': '2026-10-04T16:00:00+00:00'}, '2026-10-05'),
    ({'day': '2026-10-05', 'saved_display': '2026.10.04 09:15'}, '2026-10-05'),
    ({'day': '2026-10-05', 'saved_display': '2026.10.04 09:15',
      'saved_display_timezone': 'Asia/Seoul'}, None),
    ({'saved_at': '2026-10-05T09:15:00'}, None),
    ({'verified_date': '2026-10-05', 'as_of_date': '2026-10-05'}, None),
])
def test_receipt_migration_uses_only_actual_consistent_save_evidence(record, expected):
    from blogbot.planning import receipt_day
    if expected:
        assert receipt_day(record) == expected
    else:
        with pytest.raises(ValueError):
            receipt_day(record)


@pytest.mark.parametrize('status', ['SAVING', 'SAVE_UNCERTAIN'])
def test_uncertain_local_save_cannot_be_exported_as_another_handoff(tmp_path, monkeypatch, status):
    settings = settings_on(tmp_path, monkeypatch)
    write_packet(settings, plan_draft(settings, 'uncertain', status=status))
    write_packet(settings, plan_draft(settings, 'approved'))
    monkeypatch.setenv('BLOG_BUNDLE_KEY', Fernet.generate_key().decode())
    cloud.pack(settings, tmp_path / 'bundle.enc')
    assert json.loads((tmp_path / 'ready.json').read_text())['posts'] == []


def test_simultaneous_media_resume_purchases_only_one_stage(tmp_path, monkeypatch):
    from threading import Event
    settings = settings_on(tmp_path, monkeypatch)
    write_packet(settings, plan_draft(settings, status='IMAGES_PENDING'))
    started, release, calls = Event(), Event(), []
    def media(settings, request, post):
        calls.append(request.id)
        started.set()
        assert release.wait(5)
        return post
    monkeypatch.setattr('blogbot.pipeline.generate_images', media)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(run_daily, settings)
        assert started.wait(5)
        try:
            second = pool.submit(run_daily, settings).result(timeout=5)
            assert second[0]['reason'] == 'media_in_progress'
        finally:
            release.set()
        assert first.result(timeout=5)[0]['status'] == 'APPROVED'
    assert calls == ['question']
    assert run_daily(settings)[0]['status'] == 'DAILY_PLAN_READY'


@pytest.mark.parametrize('consumed', [False, True])
def test_local_cli_unmet_slot_never_reports_success(tmp_path, monkeypatch, consumed):
    from blogbot import cli
    settings = settings_on(tmp_path, monkeypatch)
    if consumed:
        source = tmp_path / 'q.json'
        source.write_text(json.dumps({'id': 'consumed', 'category': 'parenting', 'question': '이전 질문'}))
        enqueue_file(settings, source)
        with connect_db(settings.db_path) as conn:
            conn.execute("INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,'SAVED_NAVER')",
                         ('2026-10-02', 'parenting', 'consumed'))
            conn.commit()
    monkeypatch.setattr(cli, 'load_settings', lambda: settings)
    monkeypatch.setattr(cli, 'telegram', lambda _: None)
    monkeypatch.setattr('sys.argv', ['blogbot', 'daily'])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1


def test_full_ai_prompts_do_not_require_irrelevant_age_tables(tmp_path, monkeypatch):
    from blogbot.editorial import routed_category_info, routed_prompt
    root = __import__('pathlib').Path(__file__).resolve().parents[1]
    settings = settings_on(tmp_path, monkeypatch)
    info = routed_category_info(settings.config['categories']['parenting'], 'parenting', 'ai_tutorial')
    assert not any(r.startswith('월령이 입력되지 않았으면') for r in info['rules'])
    assert any('개인정보' in r or '사적 정보' in r for r in info['rules'])
    for filename in ['writer.md', 'reviewer.md']:
        original = (root / 'prompts' / filename).read_text()
        routed = routed_prompt(original, 'parenting', 'ai_tutorial')
        assert '월령 | 주요 특징 | 확인할 점' not in routed
        assert '육아는 정확한 월령이 없을 때 월령별 일반 조건' not in routed
        assert '공개하지' in routed or '생략한다' in routed
        assert routed_prompt(original, 'parenting') == original


def test_midnight_holds_delivery_but_preserves_encrypted_paid_state(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    write_packet(settings, plan_draft(settings, 'paid', status='IMAGES_PENDING'))
    journal = tmp_path / 'usage.jsonl'
    journal.write_text('{"request_id":"paid","stage":"writer"}\n')
    set_clock(monkeypatch, datetime(2026, 10, 5, 15, tzinfo=UTC))
    with pytest.raises(ValueError, match='Reload'):
        run_daily(settings)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    bundle = tmp_path / 'bundle.enc'
    cloud.pack(settings, bundle)
    cloud.extract_bundle(bundle.read_bytes(), tmp_path / 'restored', key)
    ready = json.loads((tmp_path / 'restored/ready.json').read_text())
    assert ready['hold'] == 'kst_date_changed' and ready['posts'] == []
    assert ready['daily_plan']['date'] == '2026-10-06'
    assert (tmp_path / 'restored/usage.jsonl').read_bytes() == journal.read_bytes()
    with connect_db(tmp_path / 'restored/blog.db') as conn:
        assert conn.execute('SELECT status FROM posts').fetchone()[0] == 'IMAGES_PENDING'


def test_receipt_actual_day_overrides_attempt_generation_day(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    with connect_db(settings.db_path) as conn:
        conn.execute("INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,'SAVED_NAVER')",
                     ('2026-10-05', 'parenting', 'receipt'))
        conn.execute('INSERT INTO save_receipts(request_id,day,category) VALUES(?,?,?)',
                     ('receipt', '2026-10-04', 'parenting'))
        conn.commit()
        assert saved_count(conn, active_plan(settings)) == 0
        assert conn.execute('SELECT day FROM attempts').fetchone()[0] == '2026-10-05'


@pytest.mark.parametrize('stamp', [None, '2026-10-05T09:15:00'])
def test_unknown_post_save_time_holds_without_assuming_local_timezone(tmp_path, monkeypatch, stamp):
    from blogbot.planning import SaveDateRequired
    settings = settings_on(tmp_path, monkeypatch)
    ident = write_packet(settings, plan_draft(settings, status='SAVED_NAVER', age=3))
    with connect_db(settings.db_path) as conn:
        conn.execute('UPDATE posts SET draft_saved_at=? WHERE id=?', (stamp, ident))
        conn.commit()
        with pytest.raises(SaveDateRequired):
            saved_count(conn, active_plan(settings))
    result = run_daily(settings)
    assert result[0]['reason'] == 'save_date_reconciliation_required'
    monkeypatch.setenv('BLOG_BUNDLE_KEY', Fernet.generate_key().decode())
    cloud.pack(settings, tmp_path / 'bundle.enc')
    assert (tmp_path / 'bundle.enc').exists()
    assert json.loads((tmp_path / 'ready.json').read_text())['posts'] == []


def test_legacy_saved_attempt_requires_actual_date_reconciliation(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch)
    with connect_db(settings.db_path) as conn:
        conn.execute("INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,'SAVED_NAVER')",
                     ('2026-09-27', 'parenting', 'legacy-saved'))
        conn.commit()
    assert run_daily(settings)[0]['reason'] == 'save_date_reconciliation_required'
    with connect_db(settings.db_path) as conn:
        conn.execute('INSERT INTO save_receipts(request_id,day,category) VALUES(?,?,?)',
                     ('legacy-saved', '2026-09-28', 'parenting'))
        conn.commit()
        assert saved_count(conn, active_plan(settings)) == 0
        assert conn.execute('SELECT day FROM attempts').fetchone()[0] == '2026-09-27'


@pytest.mark.parametrize('arguments,exit_code', [
    ([], 1),
    (['--saved-at', '2026-10-04T09:15:00'], 1),
    (['--saved-at', '2026-10-04T09:15:00+09:00'], None),
    (['--saved-day', '2026-10-04'], None),
])
def test_manual_resolution_uses_observed_save_day_not_resolution_day(
        tmp_path, monkeypatch, arguments, exit_code):
    from blogbot import cli
    settings = settings_on(tmp_path, monkeypatch)
    ident = write_packet(settings, plan_draft(settings, status='SAVE_UNCERTAIN', age=1))
    monkeypatch.setattr(cli, 'load_settings', lambda: settings)
    monkeypatch.setattr('sys.argv', ['blogbot', 'resolve', '--id', str(ident),
                                     '--outcome', 'saved', *arguments])
    if exit_code:
        with pytest.raises(SystemExit) as result:
            cli.main()
        assert result.value.code == exit_code
    else:
        cli.main()
    with connect_db(settings.db_path) as conn:
        row = conn.execute('SELECT status,draft_saved_at FROM posts').fetchone()
        if exit_code:
            assert row['status'] == 'SAVE_UNCERTAIN'
            assert conn.execute('SELECT COUNT(*) FROM save_receipts').fetchone()[0] == 0
        else:
            assert row['status'] == 'SAVED_NAVER'
            assert conn.execute('SELECT day FROM save_receipts').fetchone()[0] == '2026-10-04'
            assert saved_count(conn, active_plan(settings)) == 0
            assert not row['draft_saved_at'] or row['draft_saved_at'].startswith('2026-10-04')


@pytest.mark.parametrize('status', ['DROP_REVIEW', 'DROP_DUPLICATE'])
def test_active_plan_local_cli_rejection_is_failure(tmp_path, monkeypatch, status):
    from blogbot import cli
    settings = settings_on(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, 'load_settings', lambda: settings)
    monkeypatch.setattr(cli, 'run_daily', lambda *a, **k: [{'status': status}])
    monkeypatch.setattr(cli, 'telegram', lambda _: None)
    monkeypatch.setattr('sys.argv', ['blogbot', 'daily'])
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1


def test_unzoned_display_never_assumes_kst_and_explicit_ui_zone_converts():
    from blogbot.planning import receipt_day
    with pytest.raises(ValueError):
        receipt_day({'saved_display': '2026.10.04 16:00'})
    assert receipt_day({'saved_display': '2026.10.04 16:00',
                        'saved_display_timezone': 'UTC'}) == '2026-10-05'
    assert receipt_day({'day': '2026-10-04',
                        'saved_display': '2026.10.04 16:00'}) == '2026-10-04'


def test_explicit_manual_draft_reserves_only_october_fifth(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, keep_reservation=True)
    plan = active_plan(settings)
    assert plan['reservation'] == {'category': 'parenting', 'kind': 'existing_owner_draft'}
    assert plan['date'] == '2026-10-05' and plan['target'] == 1
    for day in ['2026-10-08', '2026-10-12', '2026-10-19']:
        assert 'reservation' not in resolve_plan(settings.config, date.fromisoformat(day))
    assert resolve_plan(settings.config, date(2026, 10, 4)) is None


def test_editorial_reservation_blocks_generation_recovery_and_save_without_changing_receipts(
        tmp_path, monkeypatch):
    from blogbot.pipeline import complete_media
    from blogbot.recovery import recover_rejected
    settings = settings_on(tmp_path, monkeypatch, keep_reservation=True)
    pending = plan_draft(settings, 'paid-pending', status='IMAGES_PENDING')
    post_id = write_packet(settings, pending)
    with connect_db(settings.db_path) as conn:
        conn.execute("INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,'SAVED_NAVER')",
                     ('2026-10-04', 'parenting', 'manual-existing'))
        conn.execute('INSERT INTO save_receipts(request_id,day,category) VALUES(?,?,?)',
                     ('manual-existing', '2026-10-04', 'parenting'))
        conn.commit()
        before_attempts = [tuple(r) for r in conn.execute('SELECT * FROM attempts')]
        before_receipts = [tuple(r) for r in conn.execute('SELECT * FROM save_receipts')]
    monkeypatch.setattr('blogbot.pipeline.collect_requests', lambda _: pytest.fail('No extra topic'))
    monkeypatch.setattr('blogbot.pipeline.make_writer', lambda _: pytest.fail('No Naver save'))
    monkeypatch.setattr('blogbot.pipeline.generate_images', lambda *a: pytest.fail('No paid media'))
    monkeypatch.setattr('blogbot.pipeline.BlogLLM', lambda *a: pytest.fail('No paid text'))
    monkeypatch.setattr('blogbot.recovery.BlogLLM', lambda *a: pytest.fail('No paid recovery'))
    request = ContentRequest('paid-pending', 'parenting', {'question': '추가 질문'})
    for retry in [False, True]:
        assert run_daily(settings, count=3, retry_failed=retry)[0]['status'] == 'EDITORIAL_SLOT_RESERVED'
    assert save_pending(settings)[0]['status'] == 'EDITORIAL_SLOT_RESERVED'
    assert cloud.recover_preparation(settings, [{'status': 'IMAGES_PENDING'}], 1)[0]['status'] == 'EDITORIAL_SLOT_RESERVED'
    with connect_db(settings.db_path) as conn:
        assert recover_rejected(settings, conn, [request])[0]['status'] == 'EDITORIAL_SLOT_RESERVED'
        assert complete_media(settings, conn, post_id, pending, request, {})['status'] == 'EDITORIAL_SLOT_RESERVED'
        assert reserve_attempt(conn, settings.config, 3, [request]) is None
        assert saved_count(conn, active_plan(settings)) == 0  # No fabricated Oct 5 save.
        assert ready_categories(settings, conn) == set()  # No fabricated API approval.
        assert before_attempts == [tuple(r) for r in conn.execute('SELECT * FROM attempts')]
        assert before_receipts == [tuple(r) for r in conn.execute('SELECT * FROM save_receipts')]
        assert conn.execute('SELECT status FROM posts WHERE id=?', (post_id,)).fetchone()[0] == 'IMAGES_PENDING'
    assert not (tmp_path / 'auto-recovery.json').exists()


@pytest.mark.parametrize('mode', ['prepare', 'recover'])
def test_reserved_cloud_run_does_not_seed_generate_or_claim_missing_preparation(tmp_path, monkeypatch, mode):
    settings = settings_on(tmp_path, monkeypatch, keep_reservation=True)
    with connect_db(settings.db_path) as conn:
        conn.execute("INSERT INTO attempts(day,category,request_id,status) VALUES(?,?,?,'ERROR')",
                     ('2026-10-05', 'parenting', 'unrelated-old-attempt'))
        conn.commit()
    monkeypatch.setattr('sys.argv', ['cloud', mode])
    monkeypatch.setattr(cloud, 'load_settings', lambda: settings)
    monkeypatch.setattr(cloud, 'restore', lambda _: None)
    monkeypatch.setattr(cloud, 'seed_inputs', lambda _: pytest.fail('No automatic new queue input'))
    monkeypatch.setattr(cloud, 'run_daily', lambda *a, **k: pytest.fail('No new generation'))
    monkeypatch.setattr(cloud, 'pack', lambda *a: None)
    cloud.main()
    summary = json.loads((tmp_path / 'run-summary.json').read_text())
    assert summary['failed'] is False
    assert [r['status'] for r in summary['results']] == ['EDITORIAL_SLOT_RESERVED']
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT status FROM attempts').fetchone()[0] == 'ERROR'


def test_reserved_handoff_stays_empty_even_with_matching_approved_packet(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, keep_reservation=True)
    post = plan_draft(settings)
    write_packet(settings, post)
    key = Fernet.generate_key().decode()
    monkeypatch.setenv('BLOG_BUNDLE_KEY', key)
    cloud.pack(settings, tmp_path / 'bundle.enc')
    ready_path = tmp_path / 'ready.json'
    ready = json.loads(ready_path.read_text())
    assert ready['posts'] == [] and ready['hold'] == 'editorial_slot_reserved'
    # The unpack filter independently rejects extra delivery under the reserved slot.
    ready['posts'] = [{'post': asdict(post)}]
    atomic_json(ready_path, ready)
    cloud.filter_ready(tmp_path, {'verified_date': '2026-10-05', 'records': []}, active_plan(settings))
    assert json.loads(ready_path.read_text())['posts'] == []
    with connect_db(settings.db_path) as conn:
        assert conn.execute('SELECT status FROM posts').fetchone()[0] == 'APPROVED'
        assert conn.execute('SELECT COUNT(*) FROM save_receipts').fetchone()[0] == 0


def test_next_unreserved_parenting_day_resumes_ordinary_input_requirements(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-10', keep_reservation=True)
    assert 'reservation' not in active_plan(settings)
    assert run_daily(settings)[0]['status'] == 'PLANNED_INPUT_REQUIRED'


def test_reservation_notification_is_intentional_skip_not_prepared_saved_or_failed(tmp_path, monkeypatch):
    from blogbot.notify import telegram
    settings = settings_on(tmp_path, monkeypatch, keep_reservation=True)
    path = tmp_path / 'summary.txt'
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(path))
    monkeypatch.setenv('BLOG_NOTIFY_ENABLED', 'false')
    telegram(run_daily(settings))
    report = path.read_text()
    assert '원고 준비 0건 / 네이버 임시저장 0건' in report
    assert '기존 수동 초안으로 예약된 편집 슬롯 1건' in report
    assert '보류/실패' not in report
