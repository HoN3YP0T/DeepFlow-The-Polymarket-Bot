"""Repositories against a real PostgreSQL database.

What these cover that a fake cannot: whether the constraints actually fire, and
whether the upserts are actually atomic. Both are properties of the database, not
of our code, and asserting them against a stub proves only that the stub agrees
with the assertion.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from deepflow.adapters.persistence.repositories import SqlUnitOfWork
from deepflow.core.domain import (
    BookLevel,
    Market,
    MarketSnapshot,
    OrderBook,
    OrderIntent,
    OrderRecord,
    Outcome,
)
from deepflow.core.enums import DataQuality, OrderSide, OrderStatus, OrderType, OutcomeSide
from deepflow.core.errors import ReconciliationError
from deepflow.core.types import ClientOrderKey, ClobTokenId, ConditionId

NOW = datetime(2026, 9, 12, 6, 0, tzinfo=UTC)
COND = ConditionId("0xabc123")
YES = ClobTokenId("111")
NO = ClobTokenId("222")


def _market(*, question: str = "Will it?", closed: bool = False) -> Market:
    return Market(
        condition_id=COND,
        question=question,
        outcomes=(
            Outcome(token_id=YES, label="Yes", side=OutcomeSide.YES),
            Outcome(token_id=NO, label="No", side=OutcomeSide.NO),
        ),
        active=True,
        closed=closed,
        accepting_orders=True,
        end_date=NOW + timedelta(days=30),
    )


def _book(token_id: ClobTokenId, *, bid: str, ask: str, at: datetime = NOW) -> OrderBook:
    return OrderBook(
        token_id=token_id,
        bids=(BookLevel(price=Decimal(bid), size=Decimal(100)),),
        asks=(BookLevel(price=Decimal(ask), size=Decimal(100)),),
        captured_at=at,
    )


def _intent(key: str = "key1") -> OrderIntent:
    return OrderIntent(
        client_key=ClientOrderKey(key),
        condition_id=COND,
        token_id=YES,
        side=OrderSide.BUY,
        order_type=OrderType.MARKETABLE_LIMIT,
        size_shares=Decimal(100),
        limit_price=Decimal("0.95"),
        max_slippage_bps=Decimal(50),
    )


def _record(
    key: str = "key1", *, status: OrderStatus = OrderStatus.OPEN, **kw: object
) -> OrderRecord:
    return OrderRecord(
        client_key=ClientOrderKey(key),
        order_id=None,
        status=status,
        updated_at=NOW,
        **kw,  # type: ignore[arg-type]
    )


# --- Markets --------------------------------------------------------------
async def test_market_round_trips(uow: SqlUnitOfWork) -> None:
    await uow.markets.upsert(_market())
    found = await uow.markets.get(COND)
    assert found is not None
    assert found.condition_id == COND
    assert [o.token_id for o in found.outcomes] == [YES, NO]
    assert [o.side for o in found.outcomes] == [OutcomeSide.YES, OutcomeSide.NO]


async def test_upsert_is_idempotent(uow: SqlUnitOfWork) -> None:
    """Two upserts of the same market must not produce two rows. Read-then-write
    would race here: both callers see 'absent' and both insert."""
    await uow.markets.upsert(_market())
    await uow.markets.upsert(_market(question="Changed?"))
    tracked = await uow.markets.list_tracked()
    assert len(tracked) == 1
    assert tracked[0].question == "Changed?"


async def test_upsert_preserves_first_seen(uow: SqlUnitOfWork, session: AsyncSession) -> None:
    """``created_at`` must survive an update. It is when we first saw the market,
    which is what market-age features will read; rewriting it on every refresh
    would make every market permanently new."""
    from sqlalchemy import select

    from deepflow.adapters.persistence.models import MarketRow

    await uow.markets.upsert(_market())
    await session.flush()
    first = (await session.execute(select(MarketRow.created_at))).scalar_one()

    await uow.markets.upsert(_market(question="Updated"))
    await session.flush()
    result = (await session.execute(select(MarketRow.created_at, MarketRow.updated_at))).one()
    assert result.created_at == first
    assert result.updated_at >= first


async def test_closed_markets_are_not_tracked(uow: SqlUnitOfWork) -> None:
    """A settled market cannot be traded, so carrying it means every downstream
    loop spends cycles rejecting it."""
    await uow.markets.upsert(_market(closed=True))
    assert await uow.markets.list_tracked() == ()


async def test_accepting_orders_is_not_restored_from_the_database(
    uow: SqlUnitOfWork,
) -> None:
    """Whether the venue takes orders right now is a live fact. Reading it from a
    row would let a closed book look tradeable after a restart."""
    await uow.markets.upsert(_market())
    found = await uow.markets.get(COND)
    assert found is not None and found.accepting_orders is False


async def test_missing_market_is_none(uow: SqlUnitOfWork) -> None:
    assert await uow.markets.get(ConditionId("0xnope")) is None


# --- Snapshots ------------------------------------------------------------
async def test_snapshot_writes_one_row_per_book(uow: SqlUnitOfWork) -> None:
    snapshot = MarketSnapshot(
        condition_id=COND,
        books=(_book(YES, bid="0.94", ask="0.95"), _book(NO, bid="0.05", ask="0.06")),
        captured_at=NOW,
        quality=DataQuality.FRESH,
    )
    assert await uow.snapshots.record(snapshot) == 2

    latest = await uow.snapshots.latest(YES)
    assert latest is not None
    assert latest["best_bid"] == Decimal("0.94")
    assert latest["best_ask"] == Decimal("0.95")
    assert latest["spread"] == Decimal("0.01")
    assert latest["data_quality"] == DataQuality.FRESH.value


async def test_unpriced_book_is_not_persisted(uow: SqlUnitOfWork) -> None:
    """A row saying 'observed, no price' is indistinguishable later from a market
    that genuinely emptied -- and those mean opposite things when a backtest reads
    the history back."""
    empty = OrderBook(token_id=YES, bids=(), asks=(), captured_at=NOW)
    snapshot = MarketSnapshot(condition_id=COND, books=(empty,), captured_at=NOW)
    assert await uow.snapshots.record(snapshot) == 0
    assert await uow.snapshots.latest(YES) is None


async def test_latest_returns_the_newest_row(uow: SqlUnitOfWork) -> None:
    for minutes, bid in ((0, "0.90"), (5, "0.92"), (2, "0.91")):
        await uow.snapshots.record(
            MarketSnapshot(
                condition_id=COND,
                books=(_book(YES, bid=bid, ask="0.99", at=NOW + timedelta(minutes=minutes)),),
                captured_at=NOW + timedelta(minutes=minutes),
            )
        )
    latest = await uow.snapshots.latest(YES)
    assert latest is not None and latest["best_bid"] == Decimal("0.92")


async def test_snapshot_rows_are_append_only(uow: SqlUnitOfWork) -> None:
    """Same token, same content, two calls: two rows. A time series that
    deduplicated would erase the fact that we observed the market twice."""
    snapshot = MarketSnapshot(
        condition_id=COND, books=(_book(YES, bid="0.94", ask="0.95"),), captured_at=NOW
    )
    assert await uow.snapshots.record(snapshot) == 1
    later = MarketSnapshot(
        condition_id=COND,
        books=(_book(YES, bid="0.94", ask="0.95", at=NOW + timedelta(seconds=1)),),
        captured_at=NOW + timedelta(seconds=1),
    )
    assert await uow.snapshots.record(later) == 1


# --- Orders ---------------------------------------------------------------
async def test_order_round_trips(uow: SqlUnitOfWork) -> None:
    await uow.orders.record(_record(), intent=_intent(), run_mode="PAPER")
    found = await uow.orders.get_by_client_key(ClientOrderKey("key1"))
    assert found is not None
    assert found.status is OrderStatus.OPEN


async def test_duplicate_client_key_updates_rather_than_inserting(
    uow: SqlUnitOfWork,
) -> None:
    """Database-level idempotency. A concurrent retry of the same intent cannot
    become a second order, whatever the application layer believes."""
    await uow.orders.record(_record(), intent=_intent(), run_mode="PAPER")
    await uow.orders.record(
        _record(status=OrderStatus.FILLED, filled_shares=Decimal(100)),
        intent=_intent(),
        run_mode="PAPER",
    )
    found = await uow.orders.get_by_client_key(ClientOrderKey("key1"))
    assert found is not None
    assert found.status is OrderStatus.FILLED
    assert found.filled_shares == Decimal(100)
    assert len(await uow.orders.list_unresolved()) == 0


async def test_status_update_needs_no_intent(uow: SqlUnitOfWork) -> None:
    """A fill notification carries no intent, and must not have to invent one."""
    await uow.orders.record(_record(), intent=_intent(), run_mode="PAPER")
    await uow.orders.record(_record(status=OrderStatus.CANCELLED))
    found = await uow.orders.get_by_client_key(ClientOrderKey("key1"))
    assert found is not None and found.status is OrderStatus.CANCELLED


async def test_new_order_without_an_intent_is_refused(uow: SqlUnitOfWork) -> None:
    """A row with no intent is an order nobody can say was for which token, side or
    size -- unreconcilable against the venue, which is what the table is for."""
    with pytest.raises(ReconciliationError, match="intent required"):
        await uow.orders.record(_record(key="orphan"))


@pytest.mark.parametrize(
    "status",
    [
        OrderStatus.PENDING_NEW,
        OrderStatus.OPEN,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.DELAYED,
        OrderStatus.MATCHED_UNSETTLED,
        OrderStatus.UNKNOWN,
    ],
)
async def test_unsettled_states_are_unresolved(uow: SqlUnitOfWork, status: OrderStatus) -> None:
    """DELAYED and MATCHED_UNSETTLED belong here alongside the obvious in-flight
    states: a delayed order has not matched yet, and a matched trade can still fail
    on chain. Treating either as finished on restart is how a phantom position
    survives a reboot."""
    await uow.orders.record(_record(status=status), intent=_intent(), run_mode="PAPER")
    assert len(await uow.orders.list_unresolved()) == 1


@pytest.mark.parametrize(
    "status", [OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED]
)
async def test_terminal_states_are_resolved(uow: SqlUnitOfWork, status: OrderStatus) -> None:
    await uow.orders.record(_record(status=status), intent=_intent(), run_mode="PAPER")
    assert await uow.orders.list_unresolved() == ()


async def test_intent_fields_are_not_rewritten_by_a_status_update(
    uow: SqlUnitOfWork, session: AsyncSession
) -> None:
    """An intent does not change. If a later write disagrees about size or price,
    the key was derived from different inputs and the collision is a bug to
    surface, not to overwrite."""
    from sqlalchemy import select

    from deepflow.adapters.persistence.models import OrderRow

    await uow.orders.record(_record(), intent=_intent(), run_mode="PAPER")
    different = _intent().model_copy(update={"size_shares": Decimal(999)})
    await uow.orders.record(_record(status=OrderStatus.FILLED), intent=different, run_mode="PAPER")
    await session.flush()

    size = (await session.execute(select(OrderRow.size_shares))).scalar_one()
    assert size == Decimal(100)
