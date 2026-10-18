"""Synthetic external CAS/execution evidence only: no browser, network or social actions."""
import tomllib
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from threading import Barrier, Lock

import pytest

from blogbot.work_operations import (
    ExchangeRun,
    digest,
    exchange_report,
    exchange_run,
    exchange_slot,
    reconcile_exchange_action,
)

SLOT = '2026-10-18T08:00:00+09:00'
TARGETS = {'comments': 10, 'likes': 10, 'outgoing_requests': 10}


class Ledger:
    def __init__(self):
        self.state, self.revision = {'browser_claim': None}, 0
        self.failures = 0
        self.lock = Lock()

    def read(self):
        with self.lock:
            return self.revision, deepcopy(self.state)

    def cas(self, version, state):
        with self.lock:
            if self.failures:
                self.failures -= 1
                raise OSError('synthetic persistence failure')
            if version != self.revision:
                return False
            self.state, self.revision = deepcopy(state), self.revision + 1
            return True


@pytest.fixture
def setup():
    config = tomllib.loads((Path(__file__).parents[1] / 'config/blog.toml').read_text())
    ledger = Ledger()
    clock = [datetime.fromisoformat(SLOT)]

    def run(execution='run-08', scheduled=SLOT, status=lambda _: None):
        return ExchangeRun(config, scheduled, execution, ledger.read, ledger.cas,
                           status, lambda: clock[0])

    return config, ledger, clock, run


def proof(now, kind='comments', target='post-1', native='native-1'):
    return {'kind': kind, 'target_id': target, 'native_id': native,
            'registered': True, 'owned': True, 'checked_at': now.isoformat(),
            'registered_at': now.isoformat(), 'evidence_ref': 'synthetic-native-observation'}


def terminal(now, status='failed', ended=True):
    return {'execution_id': 'old-run', 'status': status, 'browser_work_ended': ended,
            'checked_at': now.isoformat(), 'evidence_ref': 'synthetic-execution-observation'}


@pytest.mark.parametrize(('scheduled', 'now', 'phase', 'night', 'end'), [
    (SLOT, '2026-10-18T07:59:15+09:00', 'EARLY', '2026-10-17', '09:00'),
    (SLOT, '2026-10-18T08:40:00+09:00', 'WORK', '2026-10-17', '09:00'),
    (SLOT, '2026-10-18T08:55:00+09:00', 'CLOSING', '2026-10-17', '09:00'),
    (SLOT, '2026-10-18T09:00:00+09:00', 'EXPIRED', '2026-10-17', '09:00'),
    (SLOT, '2026-10-18T09:15:00+09:00', 'EXPIRED', '2026-10-17', '09:00'),
    ('2026-10-18T07:00:00+09:00', SLOT, 'EXPIRED', '2026-10-17', '08:00'),
    ('2026-10-18T00:00:00+09:00', '2026-10-17T15:05:00+00:00',
     'WORK', '2026-10-17', '01:00'),
    ('2026-11-01T00:00:00+09:00', '2026-11-01T00:05:00+09:00',
     'WORK', '2026-10-31', '01:00'),
])
def test_original_occurrence_and_kst_deadlines(setup, scheduled, now, phase, night, end):
    config, *_ = setup
    slot = exchange_slot(config, scheduled, now)
    assert slot['phase'] == phase
    assert slot['night_id'] == night + '-night'
    assert slot['finish_deadline'][11:16] == end


@pytest.mark.parametrize('slot', ['2026-10-18T09:00:00+09:00',
                                 '2026-10-18T08:01:00+09:00', '2026-10-18T08:00:00',
                                 '2026-10-17T08:00:00+09:00'])
def test_invented_or_naive_occurrences_rejected(setup, slot):
    with pytest.raises(ValueError):
        exchange_slot(setup[0], slot, setup[2][0])


@pytest.mark.parametrize('failure', [None, RuntimeError('tool error'), KeyboardInterrupt()])
def test_finally_returns_only_own_claim_on_normal_error_and_interrupt(setup, failure):
    config, ledger, clock, _ = setup
    with (pytest.raises(type(failure)) if failure else nullcontext(),
          exchange_run(config, SLOT, 'run-08', ledger.read, ledger.cas,
                       lambda _: None, lambda: clock[0], TARGETS) as run):
        assert run.acquired
        if failure:
            raise failure
        run.perform('comments', 'post-1', lambda cutoff: proof(clock[0]))
    assert ledger.state['browser_claim'] is None
    record = next(iter(ledger.state['exchange_runs'].values()))
    assert record['status'] == ('FAILED' if failure else 'COMPLETED')
    assert ledger.state['browser_releases'][-1]['reason'] == 'OWN_FINALLY'


@pytest.mark.parametrize(('expired', 'status', 'ended', 'reclaim'), [
    (False, 'failed', True, False), (True, 'running', False, False),
    (True, 'failed', False, False), (True, 'unknown', True, False),
    (True, 'failed', True, True), (True, 'completed', True, True),
    (True, 'cancelled', True, True),
])
def test_expiry_and_observed_work_termination_both_required(setup, expired, status, ended, reclaim):
    _, ledger, clock, make = setup
    ledger.state['browser_claim'] = {'execution_id': 'old-run',
                                    'expires_at': (clock[0] + timedelta(minutes=-1 if expired else 1)).isoformat()}
    original = deepcopy(ledger.state['browser_claim'])
    run = make(status=lambda _: terminal(clock[0], status, ended))
    assert run.acquire(TARGETS) is reclaim
    run.close()
    assert ledger.state['browser_claim'] == (None if reclaim else original)


@pytest.mark.parametrize('case', ['missing', 'wrong_execution', 'stale', 'future', 'lookup_error'])
def test_unknown_or_stale_execution_evidence_preserves_claim(setup, case):
    _, ledger, clock, make = setup
    ledger.state['browser_claim'] = {'execution_id': 'old-run',
                                    'expires_at': (clock[0] - timedelta(hours=1)).isoformat()}
    original = deepcopy(ledger.state['browser_claim'])
    data = terminal(clock[0])
    if case == 'wrong_execution':
        data['execution_id'] = 'some-other-run'
    if case == 'stale':
        data['checked_at'] = (clock[0] - timedelta(minutes=6)).isoformat()
    if case == 'future':
        data['checked_at'] = (clock[0] + timedelta(seconds=1)).isoformat()

    def status(_):
        if case == 'lookup_error':
            raise OSError('execution lookup unavailable')
        return None if case == 'missing' else data

    run = make(status=status)
    assert not run.acquire(TARGETS)
    run.close()
    assert ledger.state['browser_claim'] == original


def test_abnormal_exit_is_reconciled_without_replaying_its_slot(setup):
    _, ledger, clock, make = setup
    clock[0] -= timedelta(hours=1)
    old = make('old-run', '2026-10-18T07:00:00+09:00')
    assert old.acquire(TARGETS)  # Simulate process death: no finally call.
    clock[0] += timedelta(hours=1)
    fresh = make(status=lambda _: terminal(clock[0]))
    assert fresh.acquire(TARGETS)
    assert ledger.state['exchange_runs'][old.slot['slot_id']]['status'] == 'UNCONFIRMED'
    assert not make('replay', '2026-10-18T07:00:00+09:00').acquire(TARGETS)
    fresh.close()


def test_duplicate_same_execution_id_cannot_close_live_original(setup):
    _, ledger, _, make = setup
    first, duplicate = make(), make()
    assert first.acquire(TARGETS)
    original = deepcopy(ledger.state['browser_claim'])
    assert not duplicate.acquire(TARGETS)
    duplicate.close(failed=True)
    assert ledger.state['browser_claim'] == original
    assert ledger.state['exchange_runs'][first.slot['slot_id']]['status'] == 'RUNNING'
    first.close()
    assert not make('another-execution').acquire(TARGETS)


def test_concurrent_cas_only_one_execution_acquires_browser(setup):
    _, ledger, _, make = setup
    barrier = Barrier(2)
    original_read = ledger.read

    def racing_read():
        snapshot = original_read()
        barrier.wait(timeout=5)
        return snapshot

    runs = [make('run-a'), make('run-b')]
    for run in runs:
        run.read = racing_read

    def attempt(run):
        try:
            return run.acquire(TARGETS)
        except RuntimeError:
            return False

    with ThreadPoolExecutor(2) as pool:
        assert sorted(pool.map(attempt, runs)) == [False, True]
    winner = next(r for r in runs if r.acquired)
    winner.read = original_read
    winner.close()
    assert ledger.state['browser_claim'] is None


def test_registration_then_record_failure_never_resubmits_and_can_be_reconciled(setup):
    config, ledger, clock, make = setup
    run = make()
    assert run.acquire(TARGETS)
    calls = []
    receipt = proof(clock[0])

    def submit(cutoff):
        assert cutoff.isoformat() == '2026-10-18T08:55:00+09:00'
        calls.append('registered')
        ledger.failures = 1
        return receipt

    with pytest.raises(OSError):
        run.perform('comments', 'post-1', submit)
    action_id = digest({'kind': 'comments', 'target_id': 'post-1'})
    assert ledger.state['exchange_actions'][action_id]['status'] == 'UNCERTAIN'
    with pytest.raises(ValueError, match='reconciliation'):
        run.perform('comments', 'post-1', submit)
    run.close(failed=True)
    assert ledger.state['browser_claim'] is None
    assert calls == ['registered']
    before = exchange_report(config, '2026-10-17', ledger.state, clock[0])[-1]
    assert before['verified']['comments'] == 0 and before['uncertain'] == 1
    clock[0] += timedelta(hours=2)  # Read-only evidence can arrive after the slot closes.
    reconcile_exchange_action(ledger.read, ledger.cas, action_id, receipt, clock[0])
    reconcile_exchange_action(ledger.read, ledger.cas, action_id, receipt, clock[0])
    after = exchange_report(config, '2026-10-17', ledger.state, clock[0])[-1]
    assert after['verified']['comments'] == 1 and after['uncertain'] == 0
    assert calls == ['registered']


def test_failed_intent_write_does_not_call_submit_and_cleanup_retries(setup):
    _, ledger, _, make = setup
    run = make()
    assert run.acquire(TARGETS)
    ledger.failures = 1
    with pytest.raises(OSError):
        run.perform('comments', 'post-1', lambda _: pytest.fail('must not submit'))
    ledger.failures = 2
    run.close(failed=True)
    assert ledger.state['browser_claim'] is None


def test_persistent_cleanup_failure_is_visible_and_claim_preserved(setup):
    _, ledger, _, make = setup
    run = make()
    assert run.acquire(TARGETS)
    ledger.failures = 3
    with pytest.raises(OSError):
        run.close()
    assert ledger.state['browser_claim']['execution_id'] == run.execution_id


def test_late_invocation_does_not_claim_or_submit_and_reports_missing_rounds(setup):
    config, ledger, clock, make = setup
    clock[0] += timedelta(hours=1, minutes=15)
    run = make()
    assert not run.acquire(TARGETS)
    with pytest.raises(ValueError):
        run.perform('comments', 'post-1', lambda _: pytest.fail('expired'))
    run.close()
    rows = exchange_report(config, '2026-10-17', ledger.state, clock[0])
    assert len(rows) == 11
    assert [r['status'] for r in rows] == ['UNCONFIRMED'] * 10 + ['NOT_EXECUTED']
    assert ledger.state['browser_claim'] is None


def test_waiting_never_extends_cutoff_and_cas_delay_stops_submission(setup):
    _, ledger, clock, make = setup
    run = make()
    assert run.acquire(TARGETS)
    original_cas = run.compare_and_swap

    def delayed_cas(version, state):
        result = original_cas(version, state)
        clock[0] = datetime.fromisoformat('2026-10-18T08:55:00+09:00')
        return result

    run.compare_and_swap = delayed_cas
    with pytest.raises(ValueError, match='cutoff'):
        run.perform('comments', 'post-1', lambda _: pytest.fail('late submit'))
    assert next(iter(ledger.state['exchange_actions'].values()))['status'] == 'NOT_SUBMITTED'
    run.close()
    assert ledger.state['browser_claim'] is None


@pytest.mark.parametrize('field,value', [('registered', False), ('owned', False),
                                        ('native_id', ''), ('evidence_ref', ''),
                                        ('target_id', 'different-post')])
def test_unverified_receipt_never_counts(setup, field, value):
    config, ledger, clock, make = setup
    run = make()
    assert run.acquire(TARGETS)
    invalid = {**proof(clock[0]), field: value}
    with pytest.raises(ValueError):
        run.perform('comments', 'post-1', lambda _: invalid)
    run.close()
    row = exchange_report(config, '2026-10-17', ledger.state, clock[0])[-1]
    assert row['verified']['comments'] == 0 and row['uncertain'] == 1


def test_same_native_registration_counted_once_and_original_targets_preserved(setup):
    config, ledger, clock, make = setup
    run = make()
    assert run.acquire(TARGETS)
    for target in ('post-1', 'post-2'):
        run.perform('comments', target, lambda _, target=target: proof(clock[0], target=target))
    run.close()
    rows = exchange_report(config, '2026-10-17', ledger.state, clock[0])
    assert rows[-1]['verified']['comments'] == 1
    assert rows[-1]['targets'] == TARGETS
    assert rows[0]['status'] == 'UNCONFIRMED'


def test_zero_request_target_and_global_uncertainty_block_fresh_submissions(setup):
    _, _, clock, make = setup
    run = make()
    assert run.acquire({**TARGETS, 'outgoing_requests': 0})
    with pytest.raises(ValueError, match='consumed'):
        run.perform('outgoing_requests', 'neighbor-1', lambda _: pytest.fail('zero target'))
    with pytest.raises(RuntimeError):
        run.perform('comments', 'post-1', lambda _: (_ for _ in ()).throw(RuntimeError('timeout')))
    run.close(failed=True)
    clock[0] = datetime.fromisoformat('2026-10-18T22:00:00+09:00')
    following = make('next-night', clock[0].isoformat())
    assert following.acquire(TARGETS)
    with pytest.raises(ValueError, match='reconciliation'):
        following.perform('comments', 'post-1', lambda _: pytest.fail('duplicate uncertain target'))
    following.close()


def test_cleanup_never_releases_replacement_writer_claim(setup):
    _, ledger, _, make = setup
    run = make()
    assert run.acquire(TARGETS)
    writer = {'execution_id': 'writer-0915', 'invocation_token': 'other',
              'expires_at': '2026-10-18T10:15:00+09:00'}
    ledger.state['browser_claim'] = deepcopy(writer)
    run.close()
    assert ledger.state['browser_claim'] == writer


def test_early_delivery_can_wait_until_original_slot_without_early_action(setup):
    _, _, clock, make = setup
    clock[0] -= timedelta(seconds=45)
    run = make()
    assert not run.acquire(TARGETS)
    with pytest.raises(ValueError):
        run.perform('comments', 'post-1', lambda _: pytest.fail('early submission'))
    clock[0] += timedelta(seconds=45)
    assert run.acquire(TARGETS)
    run.close()


def test_delayed_acquisition_past_cutoff_returns_own_claim_immediately(setup):
    _, ledger, clock, make = setup
    run = make()
    original_cas = ledger.cas

    def delayed_cas(revision, state):
        result = original_cas(revision, state)
        clock[0] += timedelta(minutes=55)
        return result

    run.compare_and_swap = delayed_cas
    assert not run.acquire(TARGETS)
    assert ledger.state['browser_claim'] is None
    assert ledger.state['exchange_runs'][run.slot['slot_id']]['status'] == 'NOT_EXECUTED'


def test_delayed_terminal_lookup_does_not_acquire_after_deadline(setup):
    _, ledger, clock, make = setup
    ledger.state['browser_claim'] = {'execution_id': 'old-run',
                                    'expires_at': (clock[0] - timedelta(hours=1)).isoformat()}

    def delayed_status(_):
        clock[0] += timedelta(hours=1)
        return terminal(clock[0])

    run = make(status=delayed_status)
    assert not run.acquire(TARGETS)
    # Its original snapshot time cannot validate this now-future evidence.
    assert ledger.state['browser_claim']['execution_id'] == 'old-run'
    run.close()


def test_missing_shared_inventory_or_version_never_assumes_browser_free(setup):
    _, ledger, _, make = setup
    run = make()
    ledger.state = {}
    with pytest.raises(ValueError, match='Authoritative'):
        run.acquire(TARGETS)
    run.read = lambda: (None, {'browser_claim': None})
    with pytest.raises(ValueError, match='Authoritative'):
        run.acquire(TARGETS)


@pytest.mark.parametrize('claim', [{}, [], False, '', {'execution_id': 'unknown-expiry'}])
def test_malformed_claim_inventory_never_means_unoccupied(setup, claim):
    _, ledger, _, make = setup
    ledger.state['browser_claim'] = claim
    with pytest.raises(ValueError, match='Unknown browser claim'):
        make().acquire(TARGETS)
    assert ledger.state['browser_claim'] == claim


def test_old_registration_and_not_submitted_intent_cannot_become_new_performance(setup):
    _, ledger, clock, make = setup
    run = make()
    assert run.acquire(TARGETS)
    receipt = proof(clock[0])
    receipt['registered_at'] = (clock[0] - timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        run.perform('comments', 'post-1', lambda _: receipt)
    action_id = digest({'kind': 'comments', 'target_id': 'post-1'})
    ledger.state['exchange_actions'][action_id]['status'] = 'NOT_SUBMITTED'
    with pytest.raises(ValueError):
        reconcile_exchange_action(ledger.read, ledger.cas, action_id, proof(clock[0]), clock[0])
    run.close()
