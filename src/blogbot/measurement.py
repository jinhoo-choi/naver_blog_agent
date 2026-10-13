"""KST calendar-date cohorts; observation time is not publication time."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from .core import KST


def post_window(published_at: str, *, precision='second') -> dict:
    stamp = datetime.fromisoformat(published_at)
    if stamp.tzinfo is None or precision not in {'second', 'minute'}:
        raise ValueError('An observed publication time and timezone are required')
    if precision == 'minute' and (stamp.second or stamp.microsecond):
        raise ValueError('Minute publication evidence cannot invent seconds')
    start = stamp.astimezone(KST).date()
    return {'start_date': start.isoformat(), 'end_date': (start + timedelta(days=6)).isoformat(),
            'complete_not_before': (start + timedelta(days=7)).isoformat(),
            'timezone': 'Asia/Seoul', 'days': 7, 'window': 'D0~D6'}


def cohort_review_date(plan: dict) -> str:
    complete = date.fromisoformat(plan['measurement_end']) + timedelta(days=7)
    return max(complete, date.fromisoformat(plan['review_not_before'])).isoformat()
