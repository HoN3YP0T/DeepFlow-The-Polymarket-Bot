"""Clock tests. Injected time is what makes freshness logic testable and
backtests free of lookahead."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from deepflow.core.clock import ManualClock, SystemClock


def test_system_clock_is_timezone_aware_utc() -> None:
    now = SystemClock().now()
    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


def test_manual_clock_does_not_move_on_its_own() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    assert clock.now() == clock.now()


def test_manual_clock_advances_explicitly() -> None:
    clock = ManualClock(datetime(2026, 1, 1, tzinfo=UTC))
    clock.advance(timedelta(seconds=30))
    assert clock.now() == datetime(2026, 1, 1, 0, 0, 30, tzinfo=UTC)


def test_manual_clock_rejects_naive_datetimes() -> None:
    """A naive datetime silently compared against an aware one raises at the
    worst possible moment -- inside a freshness check."""
    with pytest.raises(ValueError, match="timezone-aware"):
        ManualClock(datetime(2026, 1, 1))
