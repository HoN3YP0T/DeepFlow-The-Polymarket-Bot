"""Exposure accounting.

Tracks committed capital across the dimensions the limits are expressed in.

The one that matters most is correlated exposure. Ten independent 2% positions
is a diversified book; ten positions that all resolve on the same match, the
same election, or the same BTC print is one 20% position wearing a disguise,
and the per-position limit will happily wave it through.

This module owns **state**, not policy. It answers "what is committed, and how is
it grouped"; the limits and the bankroll are passed in by :class:`RiskEngine`,
which owns them. Keeping the split means a limit change cannot silently alter
recorded exposure, and exposure can be rebuilt without consulting configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from deepflow.config.thresholds import RiskLimits
from deepflow.core.enums import MarketCategory
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId, EventId
from deepflow.ports.repository import PositionRepository

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class MarketRef:
    """What exposure needs to know about a market in order to group it.

    Deliberately not the whole :class:`~deepflow.core.domain.Market`. Exposure must
    be rebuildable from persisted positions after a restart, and a full market
    payload may no longer be fetchable by then -- a closed market can vanish from
    the catalogue while the position it produced is still open.
    """

    condition_id: ConditionId
    event_id: EventId | None = None
    category: MarketCategory = MarketCategory.UNKNOWN
    correlation_key: str | None = None
    """Explicit grouping override, for correlation the event id cannot express:
    two markets on different events that resolve on the same underlying fact."""


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

    def __init__(self, positions: PositionRepository | None = None) -> None:
        self._positions = positions
        self._snapshot = ExposureSnapshot()
        self._refs: dict[ConditionId, MarketRef] = {}

    @property
    def snapshot(self) -> ExposureSnapshot:
        return self._snapshot

    def register(self, ref: MarketRef) -> None:
        """Learn how a market groups, before any position exists on it.

        Called at classification time so :meth:`would_exceed` can answer for a
        market the book has never held. Without it the first trade on an event is
        ungrouped and the event limit does not bind until the second -- which is
        exactly one position too late.
        """
        self._refs[ref.condition_id] = ref

    def correlation_group(self, condition_id: ConditionId) -> str:
        """Group key for correlated exposure.

        Three tiers, most specific first:

        1. an explicit ``correlation_key``, for correlation no identifier captures --
           two separate events resolving on the same underlying fact
        2. the shared event, which is the floor rather than the whole story: every
           market on one fixture resolves off one result
        3. the market itself, so an ungrouped market is its own group rather than
           joining a bucket it has nothing to do with

        Tier 3 is the important default. Returning a shared constant for unknown
        markets would pool unrelated positions into one group and make the
        correlated limit bind on a coincidence; returning nothing would exempt them
        from the limit altogether. Being its own group is the only reading that is
        wrong in neither direction.
        """
        ref = self._refs.get(condition_id)
        if ref is None:
            return f"market:{condition_id}"
        if ref.correlation_key:
            return f"key:{ref.correlation_key}"
        if ref.event_id is not None:
            return f"event:{ref.event_id}"
        return f"market:{ref.condition_id}"

    def apply(self, *, ref: MarketRef, stake_usdc: Decimal, opened: bool = True) -> None:
        """Record capital committed to a market.

        Incremental, and therefore never the source of truth -- see
        :meth:`rebuild`. ``opened`` distinguishes a new position from adding to one,
        because the open-position count limits *positions*, not stakes.
        """
        self.register(ref)
        snapshot = self._snapshot
        group = self.correlation_group(ref.condition_id)

        snapshot.total_usdc += stake_usdc
        snapshot.by_market[ref.condition_id] = (
            snapshot.by_market.get(ref.condition_id, Decimal(0)) + stake_usdc
        )
        snapshot.by_category[ref.category] = (
            snapshot.by_category.get(ref.category, Decimal(0)) + stake_usdc
        )
        snapshot.by_correlation_group[group] = (
            snapshot.by_correlation_group.get(group, Decimal(0)) + stake_usdc
        )
        if ref.event_id is not None:
            snapshot.by_event[ref.event_id] = (
                snapshot.by_event.get(ref.event_id, Decimal(0)) + stake_usdc
            )
        if opened:
            snapshot.open_position_count += 1

    async def rebuild(self) -> ExposureSnapshot:
        """Recompute from persisted positions.

        Called at startup and after reconciliation. Exposure is derived state,
        so it is rebuilt from the source of truth rather than trusted across a
        restart -- an incrementally maintained counter that drifted is worse
        than no counter, because the limits still appear to be enforced.

        Cost basis, not mark value: a position's contribution is what was committed
        to it (``shares * average_entry_price``), which is the quantity the limits
        are written against. Marking to market would make exposure fall as a
        position moved in our favour and quietly free capacity for more of the same
        bet -- concentration increasing exactly when it feels safest.

        With no repository this **clears** exposure rather than preserving it. A
        tracker asked to rebuild from nothing knows nothing, and an empty snapshot
        refuses trades through the limits below, where a stale one would authorise
        them.
        """
        rebuilt = ExposureSnapshot()
        if self._positions is None:
            log.warning("exposure.rebuild_without_repository")
            self._snapshot = rebuilt
            return rebuilt

        open_positions = await self._positions.list_open()
        # Swap the snapshot in first: ``apply`` accumulates onto ``self._snapshot``,
        # so it has to be the new one before the first position is folded in.
        self._snapshot = rebuilt
        for position in open_positions:
            ref = self._refs.get(position.condition_id) or MarketRef(
                condition_id=position.condition_id
            )
            self.apply(
                ref=ref,
                stake_usdc=position.shares * position.average_entry_price,
                opened=True,
            )

        log.info(
            "exposure.rebuilt",
            positions=len(open_positions),
            total_usdc=str(rebuilt.total_usdc),
            groups=len(rebuilt.by_correlation_group),
        )
        return rebuilt

    def would_exceed(
        self,
        *,
        stake_usdc: Decimal,
        condition_id: ConditionId,
        bankroll: Decimal,
        limits: RiskLimits,
    ) -> str | None:
        """Name the limit a prospective stake would breach, or ``None``.

        Returns the *first* breach in a fixed order rather than all of them, because
        the caller's next action is identical either way and the name is for the
        journal. The order runs narrowest to broadest so the most specific true
        statement is the one recorded: "per-event cap" is more useful in a rejection
        than "total exposure cap" when both are true.

        A non-positive bankroll refuses everything. Fractional limits are meaningless
        against zero capital, and treating them as satisfied would authorise trades
        with no money behind them.
        """
        if bankroll <= 0:
            return f"bankroll {bankroll} is non-positive"
        if stake_usdc <= 0:
            return f"stake {stake_usdc} is non-positive"

        snapshot = self._snapshot
        ref = self._refs.get(condition_id) or MarketRef(condition_id=condition_id)
        group = self.correlation_group(condition_id)

        if snapshot.open_position_count >= limits.max_open_positions:
            return (
                f"open positions {snapshot.open_position_count} at cap {limits.max_open_positions}"
            )

        checks: list[tuple[str, Decimal, Decimal]] = [
            (
                "per-market cap",
                snapshot.by_market.get(condition_id, Decimal(0)) + stake_usdc,
                bankroll * limits.max_position_fraction,
            ),
        ]
        if ref.event_id is not None:
            checks.append(
                (
                    "per-event cap",
                    snapshot.by_event.get(ref.event_id, Decimal(0)) + stake_usdc,
                    bankroll * limits.max_event_exposure_fraction,
                )
            )
        checks.extend(
            [
                (
                    "correlated-group cap",
                    snapshot.by_correlation_group.get(group, Decimal(0)) + stake_usdc,
                    bankroll * limits.max_correlated_exposure_fraction,
                ),
                (
                    "per-strategy cap",
                    snapshot.by_category.get(ref.category, Decimal(0)) + stake_usdc,
                    bankroll * limits.max_strategy_exposure_fraction,
                ),
                (
                    "total exposure cap",
                    snapshot.total_usdc + stake_usdc,
                    bankroll * limits.max_total_exposure_fraction,
                ),
            ]
        )

        for name, projected, ceiling in checks:
            if projected > ceiling:
                return f"{name}: {projected} would exceed {ceiling}"
        return None
