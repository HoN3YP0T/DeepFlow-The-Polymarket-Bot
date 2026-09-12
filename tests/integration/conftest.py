"""Integration fixtures backed by a real PostgreSQL database.

Skipped entirely when no database is reachable, so the unit suite still runs on a
bare checkout. These tests exist because the things they check -- a UNIQUE
constraint actually rejecting a duplicate, an upsert actually being atomic, a
migration actually applying -- are exactly what a fake or an in-memory SQLite
stand-in cannot tell you. SQLite has different conflict semantics, no
``on conflict do update`` in the same form, and no opinion about Timescale.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from deepflow.adapters.persistence.models import Base
from deepflow.adapters.persistence.repositories import SqlUnitOfWork

DSN = os.environ.get(
    "DEEPFLOW_TEST_DSN", "postgresql+asyncpg://deepflow@127.0.0.1:5432/deepflow_test"
)


@pytest_asyncio.fixture
async def engine() -> AsyncIterator[AsyncEngine]:
    """A fresh engine per test.

    Function-scoped deliberately. asyncpg binds connections to the event loop that
    created them and pytest-asyncio gives each test its own loop, so a
    session-scoped engine hands the second test a connection attached to a dead
    loop. That surfaces as ``got Future attached to a different loop`` rather than
    as anything resembling a fixture-scope problem, which is why it is worth a
    comment.

    ``NullPool`` for the same reason: a pooled connection outliving its loop is the
    same bug wearing a different hat.
    """
    candidate = create_async_engine(DSN, poolclass=NullPool)
    try:
        async with candidate.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
    except Exception as exc:
        await candidate.dispose()
        pytest.skip(f"no test database reachable at {DSN}: {type(exc).__name__}")
    yield candidate
    await candidate.dispose()


@pytest_asyncio.fixture
async def session(engine: AsyncEngine) -> AsyncIterator[AsyncSession]:
    """A session whose work is always rolled back.

    Each test runs inside an outer transaction that is discarded, so tests cannot
    see each other's rows and the database needs no cleanup between them. The
    session joins that transaction rather than owning it, which means a test can
    call ``commit()`` on the unit of work -- and must, to exercise the constraint
    paths -- without the data actually persisting.
    """
    connection: AsyncConnection = await engine.connect()
    transaction = await connection.begin()
    factory = async_sessionmaker(
        bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with factory() as active:
        yield active
    await transaction.rollback()
    await connection.close()


@pytest.fixture
def uow(session: AsyncSession) -> SqlUnitOfWork:
    return SqlUnitOfWork(session)
