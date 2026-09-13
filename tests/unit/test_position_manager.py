"""Position manager: the books must match what actually happened.

Every test here defends one of three properties, each of which is a way the local
state could quietly diverge from the venue:

* the position is updated from the **fill**, never from the decision that asked for it;
* an unmarkable position reports **`None`**, never a plausible zero;
* nothing is silently skipped — a position with no snapshot is the state in which the
  exit engine cannot see the event it exists to react to.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from deepflow.core.clock import ManualClock
from deepflow.core.domain import (
    BookLevel,
    ClosedFill,
    ExitDecision,
    MarketSnapshot,
    OrderBook,
    Position,
)
from deepflow.core.enums import ExitAction
from deepflow.core.types import ClobTokenId, ConditionId, PositionId
from deepflow.positions.exit_engine import ExitEngine, StateChange
from deepflow.positions.manager import PositionManager

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
TOKEN = ClobTokenId("11111")
CONDITION = ConditionId("0xcond")
OTHER = ConditionId("0xother")


def _position(
    *, shares: str = "100", entry: str = "0.94", condition: ConditionId = CONDITION
) -> Position:
    return Position(
        position_id=PositionId("p1"),
        condition_id=condition,
        token_id=TOKEN,
        shares=Decimal(shares),
        average_entry_price=Decimal(entry),
        entry_probability=Decimal(entry),
        opened_at=NOW,
    )


def _snapshot(*, bid: str | None = "0.93", condition: ConditionId = CONDITION) -> MarketSnapshot:
    bids = (BookLevel(price=Decimal(bid), size=Decimal(500)),) if bid is not None else ()
    return MarketSnapshot(
        condition_id=condition,
        books=(
            OrderBook(
                token_id=TOKEN,
                bids=bids,
                asks=(BookLevel(price=Decimal("0.96"), size=Decimal(500)),),
                captured_at=NOW,
            ),
        ),
        captured_at=NOW,
    )


class _Repo:
    def __init__(self, positions: list[Position] | None = None) -> None:
        self.positions = {p.position_id: p for p in positions or []}
        self.writes: list[Position] = []

    async def upsert(self, position: Position) -> None:
        self.positions[position.position_id] = position
        self.writes.append(position)

    async def get(self, position_id: PositionId) -> Position | None:
        return self.positions.get(position_id)

    async def list_open(self) -> list[Position]:
        return [p for p in self.positions.values() if p.closed_at is None]


class _Closer:
    def __init__(self, fill: ClosedFill | None) -> None:
        self.fill = fill
        self.calls: list[dict[str, Any]] = []

    async def close(
        self, position: Position, *, fraction: Decimal, urgent: bool = False
    ) -> ClosedFill | None:
        self.calls.append({"position": position, "fraction": fraction, "urgent": urgent})
        return self.fill


class _Journal:
    def __init__(self, *, raises: bool = False) -> None:
        self.entries: list[dict[str, Any]] = []
        self.raises = raises

    async def record_exit(self, decision: ExitDecision, *, outcome: dict[str, Any]) -> None:
        if self.raises:
            raise RuntimeError("journal unavailable")
        self.entries.append({"decision": decision, "outcome": outcome})


def _manager(repo: _Repo, **kwargs: Any) -> PositionManager:
    return PositionManager(
        repository=repo,  # type: ignore[arg-type]
        exit_engine=kwargs.pop("exit_engine", ExitEngine()),
        closer=kwargs.pop("closer", None),
        journal=kwargs.pop("journal", None),
        clock=ManualClock(NOW),
    )


def _decision(
    action: ExitAction = ExitAction.FULL_EXIT, *, fraction: str = "1"
) -> ExitDecision:
    return ExitDecision(
        position_id=PositionId("p1"),
        action=action,
        exit_score=80,
        fraction=Decimal(fraction),
        reason="test",
    )


# --- Marking --------------------------------------------------------------
@pytest.mark.asyncio
async def test_the_mark_uses_the_bid_we_would_hit() -> None:
    """Not the mid. The mid of this book is 0.945, above the 0.94 entry, so marking
    there turns a losing position into a winning one on paper."""
    pnl = await _manager(_Repo()).mark_to_market(_position(), _snapshot(bid="0.93"))
    assert pnl == Decimal("-1.00")


@pytest.mark.asyncio
async def test_an_unmarkable_position_is_none_not_zero() -> None:
    """Zero would put the position into an exposure total as though it were
    break-even, and every aggregate built on it would be quietly wrong."""
    assert await _manager(_Repo()).mark_to_market(_position(), _snapshot(bid=None)) is None


# --- Sweeping -------------------------------------------------------------
@pytest.mark.asyncio
async def test_every_open_position_with_a_snapshot_is_evaluated() -> None:
    repo = _Repo([_position()])
    decisions = await _manager(repo).reevaluate_all({CONDITION: _snapshot()})
    assert [d.position_id for d in decisions] == [PositionId("p1")]


@pytest.mark.asyncio
async def test_a_position_with_no_snapshot_is_skipped_not_decided() -> None:
    """An open position whose market stopped streaming is exactly the state in which
    the exit engine cannot see the event it exists to react to. Inventing a HOLD for it
    would make a dead feed look like a calm market."""
    repo = _Repo([_position(condition=OTHER)])
    assert await _manager(repo).reevaluate_all({CONDITION: _snapshot()}) == []


@pytest.mark.asyncio
async def test_a_state_change_reaches_the_engine_through_the_sweep() -> None:
    """The wiring that makes EMERGENCY_EXIT reachable at all: it is supplied per market
    by the caller, because only the caller knows which side the position holds."""
    repo = _Repo([_position()])
    decisions = await _manager(repo).reevaluate_all(
        {CONDITION: _snapshot()},
        state_changes={CONDITION: StateChange(detail="red card")},
    )
    assert [d.action for d in decisions] == [ExitAction.EMERGENCY_EXIT]


@pytest.mark.asyncio
async def test_closed_positions_are_not_reevaluated() -> None:
    repo = _Repo([_position().model_copy(update={"closed_at": NOW})])
    assert await _manager(repo).reevaluate_all({CONDITION: _snapshot()}) == []


# --- Applying -------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_full_exit_closes_the_position_and_books_realized_pnl() -> None:
    repo = _Repo([_position()])
    closer = _Closer(ClosedFill(shares=Decimal(100), price=Decimal("0.90"), fees_usdc=Decimal(1)))
    updated = await _manager(repo, closer=closer).apply(_decision())
    assert updated is not None
    assert updated.shares == Decimal(0)
    assert updated.closed_at == NOW
    # (0.90 - 0.94) * 100 - 1 fee
    assert updated.realized_pnl == Decimal("-5.00")


@pytest.mark.asyncio
async def test_a_partial_fill_leaves_the_rest_open() -> None:
    """A PARTIAL_EXIT for half that fills a third leaves two thirds open. Writing the
    intended fraction would leave the books believing otherwise."""
    repo = _Repo([_position()])
    closer = _Closer(ClosedFill(shares=Decimal(30), price=Decimal("0.92")))
    updated = await _manager(repo, closer=closer).apply(
        _decision(ExitAction.PARTIAL_EXIT, fraction="0.5")
    )
    assert updated is not None
    assert updated.shares == Decimal(70)
    assert updated.closed_at is None
    assert updated.realized_pnl == Decimal("-0.60")


@pytest.mark.asyncio
async def test_no_fill_leaves_the_position_untouched() -> None:
    """A book that could not absorb the size leaves the position exactly as it was —
    still open, still watched."""
    repo = _Repo([_position()])
    journal = _Journal()
    updated = await _manager(repo, closer=_Closer(None), journal=journal).apply(_decision())
    assert updated is not None
    assert updated.shares == Decimal(100)
    assert repo.writes == []
    assert journal.entries[0]["outcome"] == {"applied": False, "reason": "no fill"}


@pytest.mark.asyncio
async def test_an_emergency_exit_is_marked_urgent_to_the_closer() -> None:
    """The thesis is void, so crossing the spread costs less than staying in — the
    reverse of the trade-off everywhere else, which is why it travels explicitly."""
    repo = _Repo([_position()])
    closer = _Closer(ClosedFill(shares=Decimal(100), price=Decimal("0.5")))
    await _manager(repo, closer=closer).apply(_decision(ExitAction.EMERGENCY_EXIT))
    assert closer.calls[0]["urgent"] is True


@pytest.mark.asyncio
async def test_a_normal_exit_is_not_urgent() -> None:
    repo = _Repo([_position()])
    closer = _Closer(ClosedFill(shares=Decimal(100), price=Decimal("0.92")))
    await _manager(repo, closer=closer).apply(_decision())
    assert closer.calls[0]["urgent"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("action", [ExitAction.HOLD, ExitAction.ADD])
async def test_hold_and_add_place_no_order(action: ExitAction) -> None:
    """ADD is a new trade: it goes through entry sizing, the safety gate and the risk
    engine, none of which are in scope here."""
    repo = _Repo([_position()])
    closer = _Closer(ClosedFill(shares=Decimal(100), price=Decimal("0.92")))
    await _manager(repo, closer=closer).apply(_decision(action))
    assert closer.calls == []


@pytest.mark.asyncio
async def test_a_missing_closer_records_the_decision_rather_than_dropping_it() -> None:
    """PAPER and SHADOW runs evaluate exits without an executor. A decision reached and
    not acted on must be distinguishable from one that never happened."""
    repo = _Repo([_position()])
    journal = _Journal()
    await _manager(repo, journal=journal).apply(_decision())
    assert journal.entries[0]["outcome"]["reason"] == "no closer configured"


@pytest.mark.asyncio
async def test_an_overfill_is_clamped_and_logged_not_turned_into_a_short() -> None:
    """A fill larger than the position means the venue and the local books disagree
    about size. That is reconciliation's business, not something to encode as a short."""
    repo = _Repo([_position(shares="50")])
    closer = _Closer(ClosedFill(shares=Decimal(80), price=Decimal("0.92")))
    updated = await _manager(repo, closer=closer).apply(_decision())
    assert updated is not None
    assert updated.shares == Decimal(0)
    assert updated.realized_pnl == Decimal("-1.00")


@pytest.mark.asyncio
async def test_a_journal_failure_does_not_undo_the_position_update() -> None:
    """The trade has happened either way. Losing the record of it is bad; losing the
    position update because the record failed is worse."""
    repo = _Repo([_position()])
    closer = _Closer(ClosedFill(shares=Decimal(100), price=Decimal("0.92")))
    updated = await _manager(repo, closer=closer, journal=_Journal(raises=True)).apply(_decision())
    assert updated is not None
    assert updated.shares == Decimal(0)


@pytest.mark.asyncio
async def test_applying_to_a_vanished_position_is_none_not_a_crash() -> None:
    assert await _manager(_Repo()).apply(_decision()) is None


# --- Closing without trading ----------------------------------------------
@pytest.mark.asyncio
async def test_close_marks_the_books_without_placing_an_order() -> None:
    """For a position that is already gone — resolution, or an operator correcting the
    books after reconciliation."""
    repo = _Repo([_position()])
    closer = _Closer(ClosedFill(shares=Decimal(100), price=Decimal("1")))
    closed = await _manager(repo, closer=closer).close(PositionId("p1"), reason="resolved")
    assert closed is not None
    assert closed.closed_at == NOW
    assert closed.shares == Decimal(100)
    assert closer.calls == []
