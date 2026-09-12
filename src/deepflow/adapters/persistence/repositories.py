"""Repository implementations over SQLAlchemy.

Scope note: markets, snapshots and orders are implemented (Phase 1). Positions
and the journal stay stubbed until the phases that write them, because their
column choices should follow a real writer rather than a guess.

Two conventions worth stating once, since they apply throughout:

* **Upserts, not read-then-write.** Every writer here uses PostgreSQL's
  ``on conflict do update``. The read-then-write version races: two coroutines
  both see "absent", both insert, and one gets an integrity error at a point
  where the caller has already decided it was doing an update.
* **Domain types cross the boundary here and nowhere else.** Rows are translated
  to and from ``deepflow.core.domain`` in this module, so a schema change
  surfaces as failures in one file.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from deepflow.adapters.persistence.models import (
    JournalRow,
    MarketRow,
    MarketSnapshotRow,
    OrderRow,
)
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import (
    Market,
    MarketSnapshot,
    OrderIntent,
    OrderRecord,
    Outcome,
    Position,
    Signal,
)
from deepflow.core.enums import (
    MarketCategory,
    OrderSide,
    OrderStatus,
    OrderType,
    OutcomeSide,
    ResolutionValidity,
)
from deepflow.core.errors import ReconciliationError
from deepflow.core.logging import get_logger
from deepflow.core.types import (
    ClientOrderKey,
    ClobTokenId,
    ConditionId,
    EventId,
    OrderId,
    PositionId,
)

log = get_logger(__name__)

#: Order states that are not yet settled. Read first on startup: these rows decide
#: whether the process may resume trading or must reconcile before doing anything.
UNRESOLVED_STATUSES = (
    OrderStatus.PENDING_NEW.value,
    OrderStatus.OPEN.value,
    OrderStatus.PARTIALLY_FILLED.value,
    OrderStatus.DELAYED.value,
    OrderStatus.MATCHED_UNSETTLED.value,
    OrderStatus.UNKNOWN.value,
)


class SqlMarketRepository:
    def __init__(self, session: AsyncSession, clock: Clock | None = None) -> None:
        self._session = session
        self._clock = clock or SystemClock()

    async def upsert(self, market: Market) -> None:
        """Insert or update by condition id.

        ``created_at`` is deliberately excluded from the update clause: an upsert
        of an existing market must not rewrite when we first saw it, which is what
        the market-age features will eventually read.
        """
        now = self._clock.now()
        values = {
            "condition_id": str(market.condition_id),
            "event_id": str(market.event_id) if market.event_id else None,
            "question": market.question,
            "slug": market.slug,
            "category": MarketCategory.UNKNOWN.value,
            "resolution_validity": ResolutionValidity.NOT_CHECKED.value,
            "lifecycle_state": "DISCOVERED",
            "outcomes": [
                {
                    "token_id": str(o.token_id),
                    "label": o.label,
                    "side": o.side.value if o.side else None,
                }
                for o in market.outcomes
            ],
            "active": market.active,
            "closed": market.closed,
            "end_date": market.end_date,
            "created_at": now,
            "updated_at": now,
        }
        statement = insert(MarketRow).values(**values)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[MarketRow.condition_id],
                set_={
                    key: statement.excluded[key]
                    for key in values
                    if key
                    not in (
                        "condition_id",
                        "created_at",
                        "category",
                        "resolution_validity",
                        "lifecycle_state",
                    )
                },
            )
        )

    async def record_classification(
        self,
        condition_id: ConditionId,
        *,
        category: str,
        confidence: Decimal | None,
        lifecycle_state: str,
        resolution_validity: str | None = None,
    ) -> None:
        """Write a market's classification and lifecycle state.

        Separate from :meth:`upsert` on purpose. An upsert refreshes what the venue
        says about a market and must not touch what *we* decided about it -- a
        catalogue sweep would otherwise reset every market to DISCOVERED and
        UNKNOWN every five minutes, erasing the pipeline's own progress.
        """
        values: dict[str, Any] = {
            "category": category,
            "classification_confidence": confidence,
            "lifecycle_state": lifecycle_state,
            "updated_at": self._clock.now(),
        }
        if resolution_validity is not None:
            values["resolution_validity"] = resolution_validity

        await self._session.execute(
            update(MarketRow).where(MarketRow.condition_id == str(condition_id)).values(**values)
        )

    async def get(self, condition_id: ConditionId) -> Market | None:
        row = await self._session.get(MarketRow, str(condition_id))
        return _to_market(row) if row is not None else None

    async def list_tracked(self) -> Sequence[Market]:
        """Markets still worth watching.

        Excludes closed ones: a settled market cannot be traded, and carrying it
        in the tracked set means every downstream loop spends cycles rejecting it.
        """
        result = await self._session.execute(
            select(MarketRow).where(MarketRow.closed.is_(False)).order_by(MarketRow.end_date)
        )
        return tuple(_to_market(row) for row in result.scalars())


class SqlSnapshotRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, snapshot: MarketSnapshot) -> int:
        """Append one row per priced book.

        A book with no bid and no ask is skipped rather than stored with nulls.
        Persisting it would put a row in the time series that says "this market was
        observed and had no price", which is indistinguishable later from a market
        that genuinely emptied -- and the two mean opposite things when reading a
        history back for a backtest.
        """
        rows = []
        for book in snapshot.books:
            if book.best_bid is None and book.best_ask is None:
                continue
            rows.append(
                {
                    "condition_id": str(snapshot.condition_id),
                    "token_id": str(book.token_id),
                    "best_bid": book.best_bid,
                    "best_ask": book.best_ask,
                    "mid": book.mid,
                    "spread": book.spread,
                    "liquidity": snapshot.liquidity,
                    "volume_24h": snapshot.volume_24h,
                    "book_imbalance": snapshot.microstructure.book_imbalance,
                    "flow_imbalance": snapshot.microstructure.flow_imbalance,
                    "data_quality": snapshot.quality.value,
                    "captured_at": book.captured_at,
                }
            )

        if not rows:
            return 0
        await self._session.execute(insert(MarketSnapshotRow), rows)
        return len(rows)

    async def latest(self, token_id: ClobTokenId) -> dict[str, Any] | None:
        """Most recent snapshot for one token.

        Ordered by ``captured_at`` descending, which the composite index on
        ``(token_id, captured_at desc)`` serves directly.
        """
        result = await self._session.execute(
            select(MarketSnapshotRow)
            .where(MarketSnapshotRow.token_id == str(token_id))
            .order_by(MarketSnapshotRow.captured_at.desc())
            .limit(1)
        )
        row = result.scalars().first()
        if row is None:
            return None
        return {
            "token_id": row.token_id,
            "condition_id": row.condition_id,
            "best_bid": row.best_bid,
            "best_ask": row.best_ask,
            "mid": row.mid,
            "spread": row.spread,
            "data_quality": row.data_quality,
            "captured_at": row.captured_at,
        }


class SqlOrderRepository:
    def __init__(self, session: AsyncSession, clock: Clock | None = None) -> None:
        self._session = session
        self._clock = clock or SystemClock()

    async def record(
        self, order: OrderRecord, *, intent: OrderIntent | None = None, run_mode: str = ""
    ) -> None:
        """Insert or update by client key.

        The client key is the primary key, so this is where database-level
        idempotency actually bites: a concurrent retry of the same intent cannot
        become a second row, whatever the application layer believes. Verified by
        attempting the duplicate rather than assumed from the DDL.

        Fields the venue owns -- ``order_id``, ``status``, fills -- are updated;
        the intent's own fields are not, because an intent does not change. If
        they differ, the key was derived from different inputs and the collision is
        a bug to surface rather than to overwrite.
        """
        if intent is None:
            existing = await self._session.get(OrderRow, str(order.client_key))
            if existing is None:
                # Writing a row without its intent would mean an order in the
                # database that nobody can say was for which token, side or size --
                # unreconcilable against the venue, which is the one thing this
                # table exists to support.
                raise ReconciliationError(
                    f"cannot record new order {order.client_key}: intent required on first write"
                )
            intent = OrderIntent(
                client_key=order.client_key,
                condition_id=ConditionId(existing.condition_id),
                token_id=ClobTokenId(existing.token_id),
                side=OrderSide(existing.side),
                order_type=OrderType(existing.order_type),
                size_shares=existing.size_shares,
                limit_price=existing.limit_price,
                max_slippage_bps=Decimal(0),
            )
            run_mode = run_mode or existing.run_mode

        values = {
            "client_key": str(order.client_key),
            "order_id": str(order.order_id) if order.order_id else None,
            "condition_id": str(intent.condition_id),
            "token_id": str(intent.token_id),
            "side": intent.side.value,
            "order_type": intent.order_type.value,
            "status": order.status.value,
            "size_shares": intent.size_shares,
            "limit_price": intent.limit_price,
            "filled_shares": order.filled_shares,
            "average_fill_price": order.average_fill_price,
            "run_mode": run_mode,
            "error": order.error,
            "submitted_at": order.submitted_at,
            "updated_at": order.updated_at or self._clock.now(),
        }
        statement = insert(OrderRow).values(**values)
        await self._session.execute(
            statement.on_conflict_do_update(
                index_elements=[OrderRow.client_key],
                set_={
                    key: statement.excluded[key]
                    for key in (
                        "order_id",
                        "status",
                        "filled_shares",
                        "average_fill_price",
                        "error",
                        "updated_at",
                    )
                },
            )
        )

    async def get_by_client_key(self, key: ClientOrderKey) -> OrderRecord | None:
        row = await self._session.get(OrderRow, str(key))
        return _to_order(row) if row is not None else None

    async def list_unresolved(self) -> Sequence[OrderRecord]:
        """Orders still in flight or indeterminate.

        Read first on startup: these are the rows that decide whether the process
        may resume trading or must reconcile before doing anything.

        ``MATCHED_UNSETTLED`` and ``DELAYED`` belong here alongside the obvious
        in-flight states. A matched-but-unsettled trade can still fail on chain,
        and a delayed order has not matched yet -- treating either as finished on
        restart is how a phantom position survives a reboot.
        """
        result = await self._session.execute(
            select(OrderRow)
            .where(OrderRow.status.in_(UNRESOLVED_STATUSES))
            .order_by(OrderRow.updated_at)
        )
        return tuple(_to_order(row) for row in result.scalars())


class SqlPositionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, position: Position) -> None:
        raise NotImplementedError("SqlPositionRepository.upsert")

    async def get(self, position_id: PositionId) -> Position | None:
        raise NotImplementedError("SqlPositionRepository.get")

    async def list_open(self) -> Sequence[Position]:
        raise NotImplementedError("SqlPositionRepository.list_open")


class SqlJournalRepository:
    """Append-only decision log.

    Rejections are recorded as carefully as entries. The trades taken are a biased
    sample of the opportunities seen; without the rejected set there is no way to
    tell a gate that is correctly protective from one that is simply never
    satisfied, and no way to know which threshold to move.
    """

    def __init__(self, session: AsyncSession, clock: Clock | None = None) -> None:
        self._session = session
        self._clock = clock or SystemClock()

    async def record_signal(self, signal: Signal) -> None:
        raise NotImplementedError("SqlJournalRepository.record_signal")

    async def record_decision(self, entry: dict[str, Any]) -> None:
        """Append one decision row.

        ``kind`` and ``reason`` are required; a journal row without a reason records
        that something happened and not why, which is the only part worth keeping.
        Everything else the caller passes lands in ``context`` as JSON rather than
        being dropped, so a new field does not need a migration to be recorded.
        """
        known = {"kind", "reason", "condition_id", "signal_id", "position_id", "run_mode"}
        kind = entry.get("kind")
        reason = entry.get("reason")
        if not kind or not reason:
            raise ValueError("journal entry requires both 'kind' and 'reason'")

        context = {k: _jsonable(v) for k, v in entry.items() if k not in known}
        await self._session.execute(
            insert(JournalRow).values(
                kind=str(kind),
                reason=str(reason),
                condition_id=_opt_str(entry.get("condition_id")),
                signal_id=_opt_str(entry.get("signal_id")),
                position_id=_opt_str(entry.get("position_id")),
                context=context or None,
                run_mode=str(entry.get("run_mode") or ""),
                recorded_at=self._clock.now(),
            )
        )

    async def list_recent(self, *, limit: int = 100) -> Sequence[dict[str, Any]]:
        """Newest entries first -- the order a dashboard and a post-mortem both want."""
        result = await self._session.execute(
            select(JournalRow).order_by(JournalRow.id.desc()).limit(limit)
        )
        return tuple(
            {
                "id": row.id,
                "kind": row.kind,
                "reason": row.reason,
                "condition_id": row.condition_id,
                "signal_id": row.signal_id,
                "position_id": row.position_id,
                "context": row.context,
                "run_mode": row.run_mode,
                "recorded_at": row.recorded_at,
            }
            for row in result.scalars()
        )


class SqlUnitOfWork:
    """Transaction boundary grouping the repositories.

    A fill and its journal entry commit together. Recording a position without
    the reasoning that produced it leaves an unauditable trade.
    """

    def __init__(self, session: AsyncSession, clock: Clock | None = None) -> None:
        self._session = session
        self.markets = SqlMarketRepository(session, clock)
        self.snapshots = SqlSnapshotRepository(session)
        self.orders = SqlOrderRepository(session, clock)
        self.positions = SqlPositionRepository(session)
        self.journal = SqlJournalRepository(session, clock)

    async def __aenter__(self) -> SqlUnitOfWork:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if exc[0] is not None:
            await self.rollback()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()


def _opt_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _jsonable(value: Any) -> Any:
    """Coerce a context value into something the JSON column accepts.

    ``Decimal`` and ``datetime`` are the two that appear constantly here and that
    the driver refuses. Stringifying beats dropping the field: a journal entry
    missing the number that caused the rejection is not much of a record.
    """
    if isinstance(value, Decimal | datetime):
        return str(value)
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    return value


# --- Row translation ------------------------------------------------------
def _to_market(row: MarketRow) -> Market:
    """Rebuild a :class:`Market` from its row.

    Only the fields the row carries are restored. Venue-owned values that change
    constantly -- tick size, fee schedule, matching delay -- are deliberately not
    persisted and not reconstructed here: a stale tick size read from our own
    database is worse than no tick size, because an order priced on it is rejected
    while the value looks authoritative.
    """
    outcomes = tuple(
        Outcome(
            token_id=ClobTokenId(str(entry["token_id"])),
            label=str(entry["label"]),
            side=OutcomeSide(entry["side"]) if entry.get("side") else None,
        )
        for entry in (row.outcomes or [])
    )
    return Market(
        condition_id=ConditionId(row.condition_id),
        event_id=EventId(row.event_id) if row.event_id else None,
        question=row.question,
        slug=row.slug,
        outcomes=outcomes,
        active=row.active,
        closed=row.closed,
        # Not persisted: whether the venue is accepting orders right now is a live
        # fact, and reading it from a row would let a closed book look tradeable.
        accepting_orders=False,
        end_date=row.end_date,
    )


def _to_order(row: OrderRow) -> OrderRecord:
    return OrderRecord(
        client_key=ClientOrderKey(row.client_key),
        order_id=OrderId(row.order_id) if row.order_id else None,
        status=OrderStatus(row.status),
        filled_shares=row.filled_shares,
        average_fill_price=row.average_fill_price,
        submitted_at=row.submitted_at,
        updated_at=row.updated_at,
        error=row.error,
    )
