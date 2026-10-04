"""Opt-in clocks for tests whose setup specifically exercises weekday policy."""
from datetime import UTC, datetime

import pytest


@pytest.fixture
def weekday_clock(monkeypatch):
    # Patch the shared clock's dependency so already-imported today_kst functions
    # and their fixture data agree. This exercises the pre-plan legacy policy;
    # daily-plan boundary tests use their own explicit KST dates.
    class WeekdayDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            fixed = datetime(2026, 9, 30, 3, tzinfo=UTC)
            return fixed.astimezone(tz) if tz is not None else fixed.replace(tzinfo=None)

    monkeypatch.setattr('blogbot.core.datetime', WeekdayDatetime)
