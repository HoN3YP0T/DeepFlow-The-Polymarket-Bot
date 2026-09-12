"""Phase 1 acceptance: the whole chain, live, end to end.

    .venv/bin/python scripts/verify_phase1.py

Checks the phase's own done-when criteria rather than each module in isolation:
discover markets, stream books, persist snapshots, and correctly report stale
data. The modules were each verified separately; this is the first run that wires
them together, which is a different claim.

Needs a database (DEEPFLOW_DATABASE__DSN). Public venue reads need no credentials.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from deepflow.adapters.persistence.engine import build_engine, build_session_factory
from deepflow.adapters.persistence.models import MarketRow, MarketSnapshotRow
from deepflow.adapters.persistence.repositories import SqlUnitOfWork
from deepflow.adapters.polymarket.discovery import SdkMarketDiscovery
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.adapters.polymarket.streams import PolymarketStreams
from deepflow.config.settings import Settings
from deepflow.core.clock import ManualClock, SystemClock
from deepflow.core.enums import DataQuality
from deepflow.pipeline.features import FeatureEngine

STREAM_SECONDS = 20
MARKETS = 6


async def main() -> int:
    settings = Settings()
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    failures: list[str] = []

    try:
        async with PolymarketSession(settings) as venue:
            discovery = SdkMarketDiscovery(venue, settings)
            streams = PolymarketStreams(venue, settings)
            features = FeatureEngine(settings.thresholds, SystemClock())

            # 1. Discover
            markets = await discovery.list_active_markets(limit=MARKETS)
            print(f"1. discovered {len(markets)} tradeable markets")
            if not markets:
                failures.append("discovery returned nothing")

            # 2. Persist the markets themselves
            async with factory() as session:
                uow = SqlUnitOfWork(session)
                for market in markets:
                    await uow.markets.upsert(market)
                await uow.commit()
            print(f"2. persisted {len(markets)} markets")

            # 3. Stream and persist snapshots
            tokens = [o.token_id for m in markets for o in m.outcomes]
            written = 0
            qualities: dict[DataQuality, int] = {}

            async def pump() -> None:
                nonlocal written
                async for snapshot in streams.subscribe_markets(tokens):
                    verdict = features.assess_snapshot(snapshot)
                    qualities[verdict] = qualities.get(verdict, 0) + 1
                    async with factory() as session:
                        uow = SqlUnitOfWork(session)
                        written += await uow.snapshots.record(snapshot)
                        await uow.commit()

            task = asyncio.create_task(pump())
            print(f"3. streaming for {STREAM_SECONDS}s...")
            await asyncio.sleep(STREAM_SECONDS)
            task.cancel()
            await streams.stop()

            counts = ", ".join(f"{q.value}={n}" for q, n in sorted(qualities.items()))
            print(f"   snapshot rows written: {written}   verdicts: {counts}")
            if written == 0:
                failures.append("no snapshot rows persisted")

            # 4. Read it back out of the database
            async with factory() as session:
                rows = (
                    await session.execute(select(func.count()).select_from(MarketSnapshotRow))
                ).scalar_one()
                market_rows = (
                    await session.execute(select(func.count()).select_from(MarketRow))
                ).scalar_one()

                uow = SqlUnitOfWork(session)
                latest = await uow.snapshots.latest(tokens[0])

            print(f"4. read back: {market_rows} market rows, {rows} snapshot rows")
            if latest is None:
                failures.append("latest() returned nothing for a streamed token")
            else:
                print(
                    f"   latest {tokens[0][:12]}...: bid={latest['best_bid']} "
                    f"ask={latest['best_ask']} quality={latest['data_quality']}"
                )

            # 5. Staleness must be reported, not merely tolerated. Advancing a
            # manual clock past the budget is the honest way to check this -- the
            # alternative is waiting for the venue to go quiet, which proves
            # nothing about the threshold.
            if latest is not None:
                future = FeatureEngine(
                    settings.thresholds, ManualClock(datetime.now(UTC) + timedelta(minutes=5))
                )
                book = next((b for b in _last_books(streams, tokens) if b is not None), None)
                if book is not None:
                    verdict = future.assess_quality(captured_at=book.captured_at, book=book)
                    print(f"5. same book seen 5 minutes later: {verdict.value}")
                    if verdict is not DataQuality.STALE:
                        failures.append(f"expected STALE after 5 minutes, got {verdict}")
                else:
                    failures.append("no folded book available for the staleness check")
    finally:
        await engine.dispose()

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("Phase 1 done-when satisfied: discovered, streamed, persisted, staleness reported")
    return 0


def _last_books(streams: PolymarketStreams, tokens: list[str]) -> list[object]:
    return [
        state.snapshot(as_of=streams.last_event_at)
        for token in tokens
        if (state := streams.book_for(token)) is not None
    ]


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
