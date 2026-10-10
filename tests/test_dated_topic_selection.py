"""Dated owner choices and explicit input-pending holds; no external calls."""
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from blogbot.config import load_settings
from blogbot.core import connect_db, reserve_attempt
from blogbot.inputs import ContentRequest, collect_requests, enqueue_file
from blogbot.pipeline import complete_media, run_daily, save_pending
from blogbot.planning import active_plan, resolve_plan, saved_count
from blogbot.recovery import recover_rejected


def settings_on(tmp_path, monkeypatch, day):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            stamp = datetime.fromisoformat(day+'T03:00:00+00:00')
            return stamp.astimezone(tz) if tz else stamp.replace(tzinfo=None)
    monkeypatch.setattr('blogbot.core.datetime', Clock)
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    return load_settings()


def enqueue_question(settings, tmp_path, category, identity, question):
    source = tmp_path/(identity+'.json')
    source.write_text(json.dumps({'id': identity, 'category': category, 'question': question}))
    enqueue_file(settings, source)


def test_face_pull_replaces_older_exercise_candidates_only_on_owner_date(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-08')
    enqueue_question(settings, tmp_path, 'exercise', 'old-squat', '스쿼트 자세가 궁금합니다.')
    monkeypatch.setattr('blogbot.inputs.fetch_community', lambda _: pytest.fail('No KIS on Thursday'))
    requests, notices = collect_requests(settings)
    assert not notices and len(requests) == 1
    chosen = requests[0]
    assert chosen.id == 'owner-20261008-face-pull' and chosen.category == 'exercise'
    assert chosen.data['question'].startswith('페이스풀')
    assert chosen.provenance['source'] == 'owner_input'
    assert '개인 체험을 창작하지 않습니다' in chosen.data['context']
    assert active_plan(settings)['target'] == 1 and 'reservation' not in active_plan(settings)
    assert (settings.inbox_dir/'old-squat'/'request.json').exists()  # No queue deletion.
    future = settings_on(tmp_path, monkeypatch, '2026-10-15')
    assert [r.id for r in collect_requests(future)[0]] == ['old-squat']
    assert '2026-10-15' not in future.config['topics']['scheduled']


@pytest.mark.parametrize('day', ['2026-10-10','2026-10-11'])
def test_weekend_waits_for_owner_input_before_all_cost_and_save_stages(tmp_path, monkeypatch, day):
    from blogbot.cloud import recover_preparation
    settings = settings_on(tmp_path, monkeypatch, day)
    # Model the unselected state independently of later owner-approved date inputs.
    settings.config['topics']['scheduled'].pop(day, None)
    settings.config['operating_plan']['reservations'][day] = {
        'category': 'parenting', 'kind': 'owner_input_pending'}
    settings.config['daily_plan'] = resolve_plan(settings.config)
    enqueue_question(settings, tmp_path, 'parenting', 'old-parenting', '이전 실제 육아 질문입니다.')
    plan = active_plan(settings)
    assert plan['reservation'] == {'category': 'parenting', 'kind': 'owner_input_pending'}
    for name in ['collect_requests', 'make_writer', 'BlogLLM', 'generate_images']:
        monkeypatch.setattr('blogbot.pipeline.'+name,
                            lambda *a, **k: pytest.fail('No automatic replacement or paid/save stage'))
    monkeypatch.setattr('blogbot.recovery.BlogLLM', lambda *a: pytest.fail('No paid recovery'))
    expected = {'status': 'EDITORIAL_SLOT_RESERVED', 'date': day,
                'category': 'parenting', 'reason': 'owner_input_pending'}
    for retry in [False, True]:
        assert run_daily(settings, retry_failed=retry) == [expected]
    assert save_pending(settings) == [expected]
    assert recover_preparation(settings, [{'status': 'IMAGES_PENDING'}], 1) == [expected]
    request = ContentRequest('old-parenting', 'parenting', {'question': '이전 질문'})
    with connect_db(settings.db_path) as conn:
        assert recover_rejected(settings, conn, [request]) == [expected]
        assert complete_media(settings, conn, 1, SimpleNamespace(), request, {}) == expected
        assert reserve_attempt(conn, settings.config, 1, [request]) is None
        assert saved_count(conn, plan) == 0
        assert conn.execute('SELECT COUNT(*) FROM attempts').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM save_receipts').fetchone()[0] == 0
    assert (settings.inbox_dir/'old-parenting'/'request.json').exists()


def test_manual_sunday_choice_replaces_only_selected_date_and_preserves_pending_saturday(
        tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-11')
    enqueue_question(settings, tmp_path, 'parenting', 'old-parenting', '이전 실제 육아 질문입니다.')
    monkeypatch.setattr('blogbot.inputs.fetch_community', lambda _: pytest.fail('No KIS on Sunday'))
    requests, notices = collect_requests(settings)
    assert not notices and len(requests) == 1
    chosen = requests[0]
    assert chosen.id == 'owner-20261011-100day-baby-play'
    assert chosen.category == 'parenting'
    assert chosen.data['question'] == '100일 아기, 깨어 있을 때 뭐 하고 놀아줄까?'
    assert chosen.provenance['source'] == 'owner_input'
    assert active_plan(settings)['target'] == 1
    assert 'reservation' not in active_plan(settings)
    assert (settings.inbox_dir/'old-parenting'/'request.json').exists()

    saturday = settings_on(tmp_path, monkeypatch, '2026-10-10')
    assert active_plan(saturday)['reservation'] == {
        'category': 'parenting', 'kind': 'owner_input_pending'}
    assert '2026-10-10' not in saturday.config['topics']['scheduled']
    future = settings_on(tmp_path, monkeypatch, '2026-10-17')
    assert [r.id for r in collect_requests(future)[0]] == ['old-parenting']
    assert '2026-10-17' not in future.config['topics']['scheduled']


@pytest.mark.parametrize('day', ['2026-10-17','2026-10-24'])
def test_waiting_does_not_extend_to_unrequested_future_weekends(tmp_path, monkeypatch, day):
    settings = settings_on(tmp_path, monkeypatch, day)
    plan = resolve_plan(settings.config, datetime.fromisoformat(day).date())
    assert plan['category'] == 'parenting' and plan['target'] == 1
    assert 'reservation' not in plan
    enqueue_question(settings, tmp_path, 'parenting', 'actual-input', '실제 육아 질문입니다.')
    assert [r.id for r in collect_requests(settings)[0]] == ['actual-input']


def test_input_pending_notice_never_claims_existing_or_saved_draft(tmp_path, monkeypatch):
    from blogbot.notify import telegram
    monkeypatch.setenv('GITHUB_STEP_SUMMARY', str(tmp_path/'summary.txt'))
    monkeypatch.setenv('BLOG_NOTIFY_ENABLED', 'false')
    telegram([{'status':'EDITORIAL_SLOT_RESERVED','reason':'owner_input_pending'}])
    text = (tmp_path/'summary.txt').read_text()
    assert '사용자 주제 입력 대기' in text and '저장 확인 아님' in text
    assert '기존 수동 초안' not in text and '네이버 임시저장 0건' in text


def test_prior_manual_hold_and_receipt_dates_are_unchanged(tmp_path, monkeypatch):
    settings = settings_on(tmp_path, monkeypatch, '2026-10-08')
    for day, kind in [('2026-10-06','existing_owner_draft'),
                      ('2026-10-07','owner_preparation_pending'),
                      ('2026-10-09','owner_preparation_pending')]:
        plan = resolve_plan(settings.config, datetime.fromisoformat(day).date())
        assert plan['reservation']['kind'] == kind
    records = json.loads((settings.root/'config/manual-saves.json').read_text())['records']
    receipt = next(r for r in records if r['request_id'] == 'manual-origins-silbi-20261005')
    assert receipt['day'] == '2026-10-05'
