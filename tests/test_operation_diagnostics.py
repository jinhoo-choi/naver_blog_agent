"""Waiting is distinct from deliverable readiness; measurement uses observed KST dates."""
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import pytest

from blogbot.config import load_settings
from blogbot.measurement import cohort_review_date, post_window
from blogbot.operations import notification_status, preparation_outcome, runtime_status
from blogbot.storage import atomic_json


def test_missing_input_keeps_failure_and_cannot_claim_a_deliverable():
    results = [{'status': 'PLANNED_INPUT_REQUIRED', 'category': 'origins'},
               {'status': 'PREPARATION_PARTIAL', 'missing_categories': ['origins']}]
    assert preparation_outcome(results, failed=True) == {
        'outcome': 'WAITING', 'deliverable_ready': False}
    results.insert(0, {'status': 'ERROR', 'stage': 'writer'})
    assert preparation_outcome(results, failed=True)['outcome'] == 'ERROR'


@pytest.mark.parametrize('status', ['MANUAL_CHECK_REQUIRED', 'SAVE_UNCERTAIN', 'DROP_REVIEW', 'IMAGES_PENDING'])
def test_actionable_holds_are_not_reported_as_ready_or_input_waiting(status):
    assert preparation_outcome([{'status': status}], failed=True) == {
        'outcome': 'NEEDS_ACTION', 'deliverable_ready': False}


def test_a_successful_diagnostic_or_editorial_hold_is_not_a_prepared_article():
    assert preparation_outcome([{'status': 'PROBE_PASSED'}], failed=False)['outcome'] == 'DIAGNOSTIC'
    assert preparation_outcome([{'status': 'EDITORIAL_SLOT_RESERVED'}], failed=False)['outcome'] == 'WAITING'
    assert preparation_outcome([{'status': 'APPROVED'}], failed=False, deliverable_ready=True) == {
        'outcome': 'READY', 'deliverable_ready': True}


def test_notification_diagnostics_never_return_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv('BLOG_DATA_DIR', str(tmp_path))
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', 'PRIVATE_TOKEN_SENTINEL')
    monkeypatch.setenv('TELEGRAM_CHAT_ID', 'PRIVATE_CHAT_SENTINEL')
    monkeypatch.setenv('BLOG_NOTIFY_ENABLED', 'false')
    assert notification_status() == 'DISABLED'
    monkeypatch.setenv('BLOG_NOTIFY_ENABLED', 'true')
    assert notification_status() == 'CONFIGURED'
    monkeypatch.delenv('TELEGRAM_CHAT_ID')
    assert notification_status() == 'NOT_CONFIGURED'
    status = runtime_status(load_settings())
    encoded = json.dumps(status)
    assert 'PRIVATE_' not in encoded and status['creator_collection'] == 'PAUSED'
    assert status['scheduler_next_run'] == 'not_observed'


@pytest.mark.parametrize('stamp,start,end,complete', [
    ('2026-10-25T14:59:59+00:00', '2026-10-25', '2026-10-31', '2026-11-01'),
    ('2026-10-25T15:00:00+00:00', '2026-10-26', '2026-11-01', '2026-11-02'),
    ('2026-10-25T23:59:00+09:00', '2026-10-25', '2026-10-31', '2026-11-01'),
])
def test_d0_d6_use_kst_calendar_days_instead_of_a_rolling_hour_window(stamp, start, end, complete):
    window = post_window(stamp)
    assert window['start_date'] == start and window['end_date'] == end
    assert window['complete_not_before'] == complete and window['days'] == 7


def test_unlabeled_or_invented_publication_times_do_not_define_a_cohort():
    with pytest.raises(ValueError):
        post_window('2026-10-25T23:59:00')
    with pytest.raises(ValueError):
        post_window('2026-10-25T23:59:05+09:00', precision='minute')
    assert post_window('2026-10-25T23:59:00+09:00', precision='minute')['end_date'] == '2026-10-31'
    plan = load_settings().config['operating_plan']
    assert plan['measurement_post_days'] == 7
    assert cohort_review_date(plan) == '2026-11-01'
    last = datetime.fromisoformat(plan['measurement_end']) + timedelta(days=7)
    assert last.date().isoformat() == plan['review_not_before']


def test_concurrent_checkpoint_writers_leave_one_complete_document_and_no_temporary_files(tmp_path):
    path = tmp_path / 'checkpoint.json'
    values = [{'run': n, 'data': [n] * 1000} for n in range(20)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda value: atomic_json(path, value), values))
    assert json.loads(path.read_text()) in values
    assert list(tmp_path.iterdir()) == [path]
