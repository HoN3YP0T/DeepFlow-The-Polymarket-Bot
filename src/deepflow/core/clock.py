"""Time source.

Injected rather than called directly so backtests can replay historical time
and tests can make freshness checks deterministic.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    """Abstract now()."""

    def now(self) -> datetime:
        """Current time, always timezone-aware UTC."""
        ...


class SystemClock:
    """Wall-clock time."""

    __slots__ = ()

    def now(self) -> datetime:
        return datetime.now(UTC)


class ManualClock:
    """Test/backtest clock advanced explicitly."""

    __slots__ = ("_now",)

    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock requires a timezone-aware datetime")
        self._now = start.astimezone(UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> datetime:
        self._now += delta
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment.astimezone(UTC)
