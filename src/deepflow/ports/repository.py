"""Persistence ports."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from deepflow.core.domain import (
    CalibrationSample,
    Market,
    MarketResolution,
    MarketSnapshot,
    OrderIntent,
    OrderRecord,
    Position,
    Prediction,
    Signal,
)
from deepflow.core.types import ClientOrderKey, ClobTokenId, ConditionId, PositionId


@runtime_checkable
class MarketRepository(Protocol):
    async def upsert(self, market: Market) -> None:
        """Refresh what the venue says about a market.

        Must not touch what *we* decided about it -- see
        :meth:`record_classification`.
        """
        ...

    async def record_classification(
        self,
        condition_id: ConditionId,
        *,
        category: str,
        confidence: Decimal | None,
        lifecycle_state: str,
        resolution_validity: str | None = None,
    ) -> None:
        """Write our own verdict about a market.

        Deliberately not part of :meth:`upsert`. A catalogue sweep refreshes venue
        facts every few minutes; if it also wrote our columns, every market would
        reset to DISCOVERED and UNKNOWN on each sweep and the pipeline would erase
        its own progress.
        """
        ...

    async def get(self, condition_id: ConditionId) -> Market | None: ...
    async def list_tracked(self) -> Sequence[Market]: ...


@runtime_checkable
class SnapshotRepository(Protocol):
    """Time-series writes for market state.

    Separate from :class:`MarketRepository` because the access patterns differ
    completely: markets are a small mutable set read by key, snapshots are an
    append-only stream written continuously and read by time range. Sharing one
    repository would mean one set of indexes serving both badly.
    """

    async def record(self, snapshot: MarketSnapshot) -> int:
        """Persist one snapshot, returning the number of rows written.

        A snapshot holds a book per outcome token, so one call writes several
        rows -- the count is returned rather than assumed, since a book that
        could not be priced writes nothing.
        """
        ...

    async def latest(self, token_id: ClobTokenId) -> dict[str, Any] | None: ...


@runtime_checkable
class OrderRepository(Protocol):
    async def record(
        self, order: OrderRecord, *, intent: OrderIntent | None = None, run_mode: str = ""
    ) -> None:
        """Persist an order's observed state.

        ``intent`` is required the first time a client key is written and ignored
        afterwards. :class:`OrderRecord` describes what the venue told us -- status,
        fills, errors -- and deliberately carries none of the trade's identity:
        which token, which side, what size, what price. Those come from the intent,
        which is immutable, so a later status update cannot silently rewrite what
        the order was for.
        """
        ...

    async def get_by_client_key(self, key: ClientOrderKey) -> OrderRecord | None: ...
    async def list_unresolved(self) -> Sequence[OrderRecord]:
        """Orders in PENDING_NEW/OPEN/UNKNOWN. Drives startup reconciliation."""
        ...


@runtime_checkable
class PositionRepository(Protocol):
    async def upsert(self, position: Position) -> None: ...
    async def get(self, position_id: PositionId) -> Position | None: ...
    async def list_open(self) -> Sequence[Position]: ...


@runtime_checkable
class PredictionRepository(Protocol):
    """Every probability an engine produced, kept so it can be scored later.

    Separate from :class:`JournalRepository` because the journal records what was
    *decided* and this records what was *believed*. Most predictions never become a
    decision, and those are the ones a calibration fit needs most: scoring only the
    traded subset fits the curve to the region where the system already thought it
    had an edge.
    """

    async def record(self, prediction: Prediction) -> None: ...

    async def list_samples(
        self, *, engine: str | None = None, limit: int = 100_000
    ) -> Sequence[CalibrationSample]:
        """Predictions joined to the outcomes of markets that have since settled.

        Only scored pairs are returned. An unsettled market contributes nothing --
        not a zero, which would teach the curve that every open position is a loss.
        """
        ...


@runtime_checkable
class ResolutionRepository(Protocol):
    """How markets actually settled. The other half of every calibration sample."""

    async def upsert(self, resolution: MarketResolution) -> None: ...

    async def unresolved_condition_ids(self, *, limit: int = 500) -> Sequence[ConditionId]:
        """Markets we have predicted on, that have ended, and have no outcome stored."""
        ...


@runtime_checkable
class JournalRepository(Protocol):
    """Append-only decision log. Section 23.

    Records rejections as well as entries -- the rejected set is what tells you
    whether the gates are calibrated or merely tight.
    """

    async def record_signal(self, signal: Signal) -> None: ...
    async def record_decision(self, entry: dict[str, Any]) -> None: ...
    async def list_recent(self, *, limit: int = 100) -> Sequence[dict[str, Any]]: ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Transaction boundary.

    Position updates and their journal entries commit together; a fill that is
    recorded without its reasoning is an unauditable position.
    """

    markets: MarketRepository
    snapshots: SnapshotRepository
    orders: OrderRepository
    positions: PositionRepository
    journal: JournalRepository
    predictions: PredictionRepository
    resolutions: ResolutionRepository

    async def __aenter__(self) -> UnitOfWork: ...
    async def __aexit__(self, *exc: object) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
