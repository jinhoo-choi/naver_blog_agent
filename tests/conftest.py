"""Opt-in clocks for tests whose setup specifically exercises weekday policy."""
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.fixture
def historical_profile(tmp_path, monkeypatch):
    """Exercise the preserved old policy independently of the Oct 15 replacement."""
    source = Path(__file__).parents[1] / 'config/blog.toml'
    profile = tmp_path / 'historical-blog.toml'
    profile.write_text(source.read_text().partition('\n[strategy_reset]')[0])
    monkeypatch.setenv('BLOG_CONFIG_PATH', str(profile))


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
