"""Circuit breakers. Section 21.

Breakers halt *new entries*. They deliberately do not block exits: a system
that cannot reduce risk during a failure is more dangerous than one that keeps
trading, and every listed trigger is a condition under which closing a position
may be the correct action.

Breakers latch by default (``auto_resume`` off). A breaker that re-arms itself
on a transient recovery will flap through the same failure repeatedly; a human
deciding the cause is actually fixed is the point of the mechanism.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from deepflow.config.thresholds import CircuitBreakerThresholds
from deepflow.core.clock import Clock
from deepflow.core.enums import BreakerReason
from deepflow.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class BreakerTrip:
    reason: BreakerReason
    detail: str
    tripped_at: datetime


@dataclass(slots=True)
class BreakerState:
    trips: dict[BreakerReason, BreakerTrip] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return bool(self.trips)


class CircuitBreakerRegistry:
    """Central halt authority.

    Every entry path consults this before submitting. Keeping it in one place
    means "are we allowed to trade?" has exactly one answer, rather than one
    per caller.
    """

    def __init__(self, thresholds: CircuitBreakerThresholds, clock: Clock) -> None:
        self._thresholds = thresholds
        self._clock = clock
        self._state = BreakerState()

    def trip(self, reason: BreakerReason, detail: str = "") -> BreakerTrip:
        """Open a breaker. Idempotent -- re-tripping keeps the original time,
        so the record shows when the problem actually started."""
        existing = self._state.trips.get(reason)
        if existing is not None:
            return existing
        trip = BreakerTrip(reason=reason, detail=detail, tripped_at=self._clock.now())
        self._state.trips[reason] = trip
        log.error("breaker.tripped", reason=str(reason), detail=detail)
        return trip

    def reset(self, reason: BreakerReason, *, actor: str) -> bool:
        """Close a breaker. ``actor`` is recorded in the audit log -- resuming
        trading is a sensitive action and is never anonymous."""
        removed = self._state.trips.pop(reason, None) is not None
        if removed:
            log.warning("breaker.reset", reason=str(reason), actor=actor)
        return removed

    def entries_allowed(self) -> bool:
        """Whether a new position may be opened."""
        return not self._state.is_open

    def exits_allowed(self) -> bool:
        """Always true. Risk reduction is never gated by a breaker."""
        return True

    @property
    def state(self) -> BreakerState:
        return self._state

    @property
    def open_reasons(self) -> tuple[BreakerReason, ...]:
        return tuple(self._state.trips)
