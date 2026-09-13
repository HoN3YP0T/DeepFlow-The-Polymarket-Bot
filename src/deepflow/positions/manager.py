"""Position manager.

Maintains open positions, marks them to market, and drives the exit engine's
reevaluation loop.

Marking uses the price at which the position could actually be *closed* -- the
bid we would hit, not the mid. On a wide outcome book the difference is the gap
between a position that looks profitable and one that is.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import (
    ClosedFill,
    ExitDecision,
    MarketSnapshot,
    Position,
    ProbabilityEstimate,
    SmartMoneySignal,
)
from deepflow.core.enums import ExitAction
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId, ConditionId, PositionId
from deepflow.journal.recorder import JournalRecorder
from deepflow.ports.execution import PositionCloserPort
from deepflow.ports.repository import PositionRepository
from deepflow.positions.exit_engine import ExitEngine, StateChange

log = get_logger(__name__)


class PositionManager:
    """Tracks and reevaluates open positions.

    Owns position *state* -- what is held, what it is worth, what should happen to it --
    and delegates the order that acts on a decision to :class:`PositionCloserPort`.
    That split is why this module imports no tick grid, fee schedule or order type: a
    class that decides whether to hold should not also be deciding order semantics.
    """

    def __init__(
        self,
        *,
        repository: PositionRepository,
        exit_engine: ExitEngine,
        closer: PositionCloserPort | None = None,
        journal: JournalRecorder | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._repository = repository
        self._exit_engine = exit_engine
        self._closer = closer
        self._journal = journal
        self._clock = clock or SystemClock()

    async def reevaluate_all(
        self,
        snapshots: Mapping[ConditionId, MarketSnapshot],
        *,
        estimates: Mapping[ClobTokenId, ProbabilityEstimate] | None = None,
        smart_money: Mapping[ConditionId, SmartMoneySignal] | None = None,
        state_changes: Mapping[ConditionId, StateChange] | None = None,
    ) -> list[ExitDecision]:
        """Reevaluate every open position for which a snapshot is available.

                Runs on every relevant market update, not on a timer. The events this system
                must react to -- a goal, a wicket, a break of serve -- are instantaneous, and a
                polling interval is a window in which a position is held against known-bad
                information.

                A position with **no snapshot is skipped, not held silently**: it is logged at
                warning level, because an open position whose market stopped streaming is the
                exact state in which the exit engine cannot see the event it exists to react to.
                Skipping quietly would make a dead feed look like a calm market.

        ``state_changes`` is the only input that can produce an EMERGENCY_EXIT, and it is
                passed in rather than derived here: it comes from the live-game sweep or an event
                pipeline, and whether a change runs *against* a position depends on which side
                the position holds. Without it the exit path is price-derived only -- which is
                exactly the gap that made the emergency branch unreachable before it was wired.

                Decisions are returned rather than applied. The caller decides what to act on,
                which keeps a sweep over fifty positions from firing fifty orders inside a loop
                that has no view of aggregate risk.
        """
        decisions: list[ExitDecision] = []
        for position in await self._repository.list_open():
            snapshot = snapshots.get(position.condition_id)
            if snapshot is None:
                log.warning(
                    "positions.no_snapshot",
                    position_id=str(position.position_id),
                    condition_id=str(position.condition_id),
                )
                continue
            decisions.append(
                await self._exit_engine.evaluate(
                    position=position,
                    snapshot=snapshot,
                    estimate=(estimates or {}).get(position.token_id),
                    smart_money=(smart_money or {}).get(position.condition_id),
                    state_change=(state_changes or {}).get(position.condition_id),
                )
            )
        return decisions

    async def mark_to_market(self, position: Position, snapshot: MarketSnapshot) -> Decimal | None:
        """Unrealized P&L at the realistic exit price, or ``None`` when unmarkable.

        Marks at the **bid we would hit**, not the mid. On a wide outcome book the
        difference is the gap between a position that looks profitable and one that is,
        and the mid is a price at which nobody has offered to buy anything.

        ``None`` rather than zero when there is no bid. A missing price is not a flat
        P&L: reporting zero would put an unmarkable position into an exposure total as
        though it were break-even, and every aggregate built on it would be quietly
        wrong.
        """
        book = snapshot.book_for(position.token_id)
        bid = book.best_bid if book is not None else None
        if bid is None:
            log.warning(
                "positions.unmarkable",
                position_id=str(position.position_id),
                reason="no bid on the outcome book",
            )
            return None
        return position.unrealized_pnl(bid)

    async def apply(self, decision: ExitDecision) -> Position | None:
        """Act on an exit decision, and record what it actually achieved.

        Returns the position as it now stands -- reduced, closed, or unchanged -- or
        ``None`` when the position is gone from the repository.

        Three rules hold here, and each is a way this could silently lie:

        * **The position is updated from the fill, never from the decision.** A
          PARTIAL_EXIT for half that fills a third leaves two thirds open, and writing
          the intended fraction would leave the books believing otherwise.
        * **No fill means no change.** A book that could not absorb the size leaves the
          position exactly as it was, still open and still watched.
        * **The journal is written whatever happens**, including for a HOLD that was
          reconsidered and for an exit that failed to fill. The rejected and unfilled
          set is what shows whether the noise band is calibrated or merely tight.
        """
        position = await self._repository.get(decision.position_id)
        if position is None:
            log.warning("positions.missing", position_id=str(decision.position_id))
            return None

        if decision.action in (ExitAction.HOLD, ExitAction.ADD):
            # ADD is a new trade and goes through entry sizing, the safety gate and the
            # risk engine -- it is deliberately not opened from here, where none of
            # those are in scope.
            await self._record(decision, {"applied": False, "action": decision.action.value})
            return position

        fraction = decision.fraction if decision.fraction > 0 else Decimal(1)
        urgent = decision.action is ExitAction.EMERGENCY_EXIT

        if self._closer is None:
            # Not an error: PAPER and SHADOW runs evaluate exits without an executor.
            # Recorded so the journal shows a decision that was reached and not acted on,
            # which is otherwise indistinguishable from one that never happened.
            await self._record(decision, {"applied": False, "reason": "no closer configured"})
            return position

        fill = await self._closer.close(position, fraction=fraction, urgent=urgent)
        if fill is None:
            await self._record(decision, {"applied": False, "reason": "no fill"})
            return position

        updated = self._reduced(position, fill)
        await self._repository.upsert(updated)
        await self._record(
            decision,
            {
                "applied": True,
                "shares_closed": str(fill.shares),
                "price": str(fill.price),
                "fees_usdc": str(fill.fees_usdc),
                "realized_pnl": str(updated.realized_pnl - position.realized_pnl),
                "shares_remaining": str(updated.shares),
                "closed": updated.closed_at is not None,
            },
        )
        return updated

    async def close(self, position_id: PositionId, *, reason: str) -> Position | None:
        """Mark a position closed without trading -- resolution, or manual override.

        Deliberately does **not** place an order. It is for a position that is already
        gone: the market resolved, or an operator is correcting the books after
        reconciliation. Using it in place of an exit would leave real shares held at the
        venue with nothing in the local state watching them, which is the divergence
        reconciliation exists to catch.
        """
        position = await self._repository.get(position_id)
        if position is None:
            log.warning("positions.missing", position_id=str(position_id))
            return None
        closed = position.model_copy(update={"closed_at": self._clock.now()})
        await self._repository.upsert(closed)
        log.info("positions.closed", position_id=str(position_id), reason=reason)
        return closed

    # --- Internals --------------------------------------------------------
    def _reduced(self, position: Position, fill: ClosedFill) -> Position:
        """The position after a fill, with realized P&L booked on the shares that left.

        Realized P&L is ``(exit - entry) * shares_closed``, less fees. Only on the
        shares that actually closed: booking the whole position's move on a partial
        exit reports profit that has not been taken.

        A fill larger than the position is clamped rather than allowed to produce
        negative shares, and logged -- it means the venue and the local books disagree
        about size, which is reconciliation's business, not something to encode as a
        short position.
        """
        shares_closed = fill.shares
        if shares_closed > position.shares:
            log.error(
                "positions.overfill",
                position_id=str(position.position_id),
                held=str(position.shares),
                closed=str(fill.shares),
            )
            shares_closed = position.shares

        realized = (fill.price - position.average_entry_price) * shares_closed - fill.fees_usdc
        remaining = position.shares - shares_closed
        update: dict[str, object] = {
            "shares": remaining,
            "realized_pnl": position.realized_pnl + realized,
        }
        if remaining == 0:
            update["closed_at"] = self._clock.now()
        return position.model_copy(update=update)

    async def _record(self, decision: ExitDecision, outcome: dict[str, Any]) -> None:
        """Journal an exit decision and its outcome, never raising.

        A journal failure must not undo a position update or abort an exit: the trade
        has happened either way, and losing the record of it is worse than losing the
        reasoning about it.
        """
        if self._journal is None:
            return
        try:
            await self._journal.record_exit(decision, outcome=outcome)
        except Exception as exc:  # the journal is a record, not a dependency
            log.error("positions.journal_failed", error=f"{type(exc).__name__}: {exc}")
