"""Repository implementations over SQLAlchemy.

Skeleton: signatures and the unit-of-work boundary are fixed so call sites can
be written and tested against fakes; the SQL is filled in per table.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from deepflow.core.domain import Market, OrderRecord, Position, Signal
from deepflow.core.types import ClientOrderKey, ConditionId, PositionId


class SqlMarketRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, market: Market) -> None:
        raise NotImplementedError("SqlMarketRepository.upsert")

    async def get(self, condition_id: ConditionId) -> Market | None:
        raise NotImplementedError("SqlMarketRepository.get")

    async def list_tracked(self) -> Sequence[Market]:
        raise NotImplementedError("SqlMarketRepository.list_tracked")


class SqlOrderRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record(self, order: OrderRecord) -> None:
        raise NotImplementedError("SqlOrderRepository.record")

    async def get_by_client_key(self, key: ClientOrderKey) -> OrderRecord | None:
        raise NotImplementedError("SqlOrderRepository.get_by_client_key")

    async def list_unresolved(self) -> Sequence[OrderRecord]:
        """Orders still in flight or indeterminate.

        Read first on startup: these are the rows that decide whether the
        process may resume trading or must reconcile before doing anything.
        """
        raise NotImplementedError("SqlOrderRepository.list_unresolved")


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
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def record_signal(self, signal: Signal) -> None:
        raise NotImplementedError("SqlJournalRepository.record_signal")

    async def record_decision(self, entry: dict[str, Any]) -> None:
        raise NotImplementedError("SqlJournalRepository.record_decision")

    async def list_recent(self, *, limit: int = 100) -> Sequence[dict[str, Any]]:
        raise NotImplementedError("SqlJournalRepository.list_recent")


class SqlUnitOfWork:
    """Transaction boundary grouping the repositories.

    A fill and its journal entry commit together. Recording a position without
    the reasoning that produced it leaves an unauditable trade.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self.markets = SqlMarketRepository(session)
        self.orders = SqlOrderRepository(session)
        self.positions = SqlPositionRepository(session)
        self.journal = SqlJournalRepository(session)

    async def __aenter__(self) -> SqlUnitOfWork:
        return self

    async def __aexit__(self, *exc: object) -> None:
        if exc[0] is not None:
            await self.rollback()

    async def commit(self) -> None:
        await self._session.commit()

    async def rollback(self) -> None:
        await self._session.rollback()
