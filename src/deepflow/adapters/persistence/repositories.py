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
    CalibrationFitRow,
    JournalRow,
    MarketResolutionRow,
    MarketRow,
    MarketSnapshotRow,
    OrderRow,
    PredictionRow,
)
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import (
    CalibrationSample,
    Market,
    MarketResolution,
    MarketSnapshot,
    OrderIntent,
    OrderRecord,
    Outcome,
    Position,
    Prediction,
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


class SqlPredictionRepository:
    """Every estimate an engine produced, and the join that scores them.

    Append-only: a prediction is a record of what was believed at a moment, and an
    upsert here would mean the most recent estimate quietly overwrote the earlier
    ones on the same market -- destroying exactly the horizon variation a fit needs
    to see.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, prediction: Prediction) -> None:
        await self._session.execute(
            insert(PredictionRow).values(
                engine=prediction.engine,
                category=prediction.category.value,
                condition_id=str(prediction.condition_id),
                token_id=str(prediction.token_id),
                model_probability=prediction.model_probability,
                calibrated_probability=prediction.calibrated_probability,
                uncertainty=prediction.uncertainty,
                horizon_seconds=prediction.horizon_seconds,
                predicted_at=prediction.predicted_at,
            )
        )

    async def list_samples(
        self, *, engine: str | None = None, limit: int = 100_000
    ) -> Sequence[CalibrationSample]:
        """Predictions joined to their market's settled payout.

        An **inner** join, deliberately. A left join filled with zero for unsettled
        markets would read as "predicted 0.9, outcome 0" -- teaching the curve that
        every still-open position was a loss, and doing it most aggressively to the
        engines whose markets run longest.

        The join is on ``(condition_id, token_id)`` rather than condition alone: a
        binary market resolves both its tokens, one to 1 and one to 0, and matching
        on the market would score every prediction against whichever outcome row the
        planner happened to return.
        """
        statement = (
            select(PredictionRow, MarketResolutionRow.payout)
            .join(
                MarketResolutionRow,
                (PredictionRow.condition_id == MarketResolutionRow.condition_id)
                & (PredictionRow.token_id == MarketResolutionRow.token_id),
            )
            .order_by(PredictionRow.predicted_at.desc())
            .limit(limit)
        )
        if engine is not None:
            statement = statement.where(PredictionRow.engine == engine)

        result = await self._session.execute(statement)
        return tuple(
            CalibrationSample(
                predicted=row.model_probability,
                realized=payout,
                engine=row.engine,
                condition_id=ConditionId(row.condition_id),
                token_id=ClobTokenId(row.token_id),
                predicted_at=row.predicted_at,
                horizon_seconds=row.horizon_seconds,
            )
            for row, payout in result.all()
        )


class SqlResolutionRepository:
    """How markets settled, one row per outcome token."""

    def __init__(self, session: AsyncSession, clock: Clock | None = None) -> None:
        self._session = session
        self._clock = clock or SystemClock()

    async def upsert(self, resolution: MarketResolution) -> None:
        """Write one row per outcome.

        Upserted rather than inserted once because a resolution can change: a
        disputed market can be reproposed and settle the other way. Overwriting is
        right -- the venue's current answer is the one that paid -- and it is also
        why ``was_disputed`` is stored, since a sample drawn from a resolution that
        moved deserves a second look.
        """
        now = self._clock.now()
        for entry in resolution.payouts:
            values = {
                "condition_id": str(resolution.condition_id),
                "token_id": str(entry.token_id),
                "payout": entry.payout,
                "status": resolution.status,
                "was_disputed": resolution.was_disputed,
                "source": resolution.source,
                "resolved_at": resolution.resolved_at,
                "recorded_at": now,
            }
            statement = insert(MarketResolutionRow).values(**values)
            await self._session.execute(
                statement.on_conflict_do_update(
                    index_elements=[
                        MarketResolutionRow.condition_id,
                        MarketResolutionRow.token_id,
                    ],
                    set_={
                        key: statement.excluded[key]
                        for key in values
                        if key not in ("condition_id", "token_id")
                    },
                )
            )

    async def unresolved_condition_ids(self, *, limit: int = 500) -> Sequence[ConditionId]:
        """Markets worth asking the venue about.

        Scoped to markets **we predicted on**: resolutions for anything else are not
        wrong, just useless, and the venue's batch limit is 20 ids per request, so
        sweeping every closed market on the venue would spend the whole budget on
        rows no sample will ever reference.

        Ordered oldest-ended first so a backlog drains in the order it accumulated
        rather than re-asking about the same recent markets on every pass.
        """
        resolved = select(MarketResolutionRow.condition_id)
        statement = (
            select(PredictionRow.condition_id, MarketRow.end_date)
            .join(MarketRow, MarketRow.condition_id == PredictionRow.condition_id)
            .where(PredictionRow.condition_id.not_in(resolved))
            .where(MarketRow.end_date.is_not(None))
            .where(MarketRow.end_date < self._clock.now())
            .group_by(PredictionRow.condition_id, MarketRow.end_date)
            .order_by(MarketRow.end_date)
            .limit(limit)
        )
        result = await self._session.execute(statement)
        return tuple(ConditionId(row[0]) for row in result.all())


class SqlCalibrationRepository:
    """Fitted curves, stored beside the evidence that produced them.

    In the database rather than a file because "which curve was live when this trade
    was sized" is a question a post-mortem will ask, and a file on an operator's
    laptop cannot answer it. Superseded fits are kept: ``active`` moves, nothing is
    deleted, so a curve that made things worse can be compared against the one that
    replaced it.
    """

    def __init__(self, session: AsyncSession, clock: Clock | None = None) -> None:
        self._session = session
        self._clock = clock or SystemClock()

    async def save(
        self,
        *,
        engine: str,
        knots: dict[str, Any],
        samples: int,
        markets: int,
        brier_before: Decimal,
        brier_after: Decimal,
        ece_before: Decimal,
        ece_after: Decimal,
        activate: bool = False,
    ) -> None:
        """Store a fit, optionally making it the live one for this engine.

        Activation is explicit and never automatic. A fit that improves its own
        training scores can still be the wrong thing to run -- fitted on one regime,
        or with no support in the band actually traded -- and that judgement belongs
        to whoever reads the report, not to the fitter that produced it.
        """
        if activate:
            await self._session.execute(
                update(CalibrationFitRow)
                .where(CalibrationFitRow.engine == engine)
                .values(active=False)
            )
        await self._session.execute(
            insert(CalibrationFitRow).values(
                engine=engine,
                knots=knots,
                samples=samples,
                markets=markets,
                brier_before=brier_before,
                brier_after=brier_after,
                ece_before=ece_before,
                ece_after=ece_after,
                active=activate,
                fitted_at=self._clock.now(),
            )
        )

    async def active_fits(self) -> dict[str, dict[str, Any]]:
        """The live curve per engine, as stored.

        Returns the raw payloads rather than calibrators so this module keeps its one
        job -- rows in, rows out -- and the engines package stays free of persistence.
        """
        result = await self._session.execute(
            select(CalibrationFitRow).where(CalibrationFitRow.active.is_(True))
        )
        return {row.engine: dict(row.knots) for row in result.scalars()}


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
        """Record a signal's existence, independently of what was decided about it.

        Routed through :meth:`record_decision` rather than given its own table. A
        signal and the decision on it belong in one ordered stream: querying "what
        did the system see, and what did it do about it" across two tables means
        reconstructing an interleaving that was never stored, and the interleaving is
        the part a post-mortem actually needs.

        ``reason`` carries the signal's own rationale, which is why the *model*
        produced it -- distinct from the gate's reason for allowing or refusing it.
        """
        await self.record_decision(
            {
                "kind": "SIGNAL",
                "reason": signal.rationale or "signal generated",
                "signal_id": str(signal.signal_id),
                "condition_id": str(signal.condition_id),
                "token_id": str(signal.token_id),
                "action": str(signal.action),
                "category": str(signal.category),
                "target_price": signal.target_price,
                "generated_at": signal.generated_at,
                "calibrated_probability": signal.probability.calibrated_probability,
                "engine": signal.probability.engine,
                "net_ev": signal.ev.net_ev,
            }
        )

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
        self.predictions = SqlPredictionRepository(session)
        self.resolutions = SqlResolutionRepository(session, clock)
        self.calibration = SqlCalibrationRepository(session, clock)

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
