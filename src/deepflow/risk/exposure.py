"""Exposure accounting.

Tracks committed capital across the dimensions the limits are expressed in.

The one that matters most is correlated exposure. Ten independent 2% positions
is a diversified book; ten positions that all resolve on the same match, the
same election, or the same BTC print is one 20% position wearing a disguise,
and the per-position limit will happily wave it through.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from deepflow.core.enums import MarketCategory
from deepflow.core.types import ConditionId, EventId


@dataclass(slots=True)
class ExposureSnapshot:
    """Current committed capital, by dimension."""

    total_usdc: Decimal = Decimal(0)
    by_event: dict[EventId, Decimal] = field(default_factory=dict)
    by_category: dict[MarketCategory, Decimal] = field(default_factory=dict)
    by_market: dict[ConditionId, Decimal] = field(default_factory=dict)
    by_correlation_group: dict[str, Decimal] = field(default_factory=dict)
    open_position_count: int = 0


class ExposureTracker:
    """Maintains exposure and answers "would this trade fit?"."""

    def __init__(self) -> None:
        self._snapshot = ExposureSnapshot()

    @property
    def snapshot(self) -> ExposureSnapshot:
        return self._snapshot

    async def rebuild(self) -> ExposureSnapshot:
        """Recompute from persisted positions.

        Called at startup and after reconciliation. Exposure is derived state,
        so it is rebuilt from the source of truth rather than trusted across a
        restart -- an incrementally maintained counter that drifted is worse
        than no counter, because the limits still appear to be enforced.
        """
        raise NotImplementedError("ExposureTracker.rebuild")

    def correlation_group(self, condition_id: ConditionId) -> str:
        """Group key for correlated exposure.

        TODO(skeleton): derive from shared event, shared underlying (one match,
        one election, one BTC print) and cross-market relations. Markets on the
        same event are the floor, not the whole story.
        """
        raise NotImplementedError("ExposureTracker.correlation_group")

    def would_exceed(self, *, stake_usdc: Decimal, condition_id: ConditionId) -> str | None:
        """Name the limit a prospective stake would breach, or ``None``."""
        raise NotImplementedError("ExposureTracker.would_exceed")
