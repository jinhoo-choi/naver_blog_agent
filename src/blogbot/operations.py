"""Read-only runtime diagnostics and truthful preparation outcomes."""
from __future__ import annotations

import os


def preparation_outcome(results, *, failed, deliverable_ready=False):
    statuses = {r.get('status') for r in results}
    if 'ERROR' in statuses or 'UNRESOLVED_FAILED_ATTEMPTS' in statuses:
        outcome = 'ERROR'
    elif 'PROBE_PASSED' in statuses and not failed:
        outcome = 'DIAGNOSTIC'
    elif deliverable_ready and not failed:
        outcome = 'READY'
    elif statuses <= {'PLANNED_INPUT_REQUIRED', 'EDITORIAL_SLOT_RESERVED', 'AUTONOMOUS_WORK_REQUIRED',
                       'PREPARATION_PARTIAL', 'NO_ELIGIBLE_INPUT_OR_DAILY_LIMIT',
                       'DAILY_PLAN_LIMIT', 'COMMUNITY_SOURCE_PENDING', 'NO_ELIGIBLE_INVESTMENT'}:
        outcome = 'WAITING'
    else:
        outcome = 'NEEDS_ACTION'
    return {'outcome': outcome, 'deliverable_ready': outcome == 'READY'}


def notification_status():
    if os.getenv('BLOG_NOTIFY_ENABLED', 'false').lower() != 'true':
        return 'DISABLED'
    return ('CONFIGURED' if os.getenv('TELEGRAM_BOT_TOKEN') and os.getenv('TELEGRAM_CHAT_ID')
            else 'NOT_CONFIGURED')


def runtime_status(settings):
    from .work_operations import exclusions, pending_writes, records
    config = settings.config
    operations = config.get('operations', {})
    from .planning import work_strategy
    strategy = work_strategy(config)
    return {'contract_version': strategy['version'] if strategy else operations.get('version'),
            'work_strategy': strategy,
            'automatic_enabled': operations.get('automatic_enabled', True),
            'manual_mode': 'separate_draft_only',
            'manual_operations': len(records(settings.db_path.parent)),
            'automatic_exclusions': sorted(exclusions(settings)),
            'pending_manual_writes': pending_writes(settings),
            'creator_collection': ('ENABLED' if operations.get('creator_collection_enabled', False)
                                   else 'PAUSED'),
            'telegram': notification_status(),
            'measurement': ({k: config['strategy_reset'].get(k) for k in
                             ('start_date', 'measurement_end', 'final_cohort_review_not_before')}
                            if strategy else {k: config.get('operating_plan', {}).get(k) for k in
                            ('measurement_window', 'measurement_post_days', 'review_not_before')}),
            'browser_coordination': 'external_versioned_ledger_required',
            'scheduler_next_run': 'not_observed'}
