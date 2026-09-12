"""Phase 1 item 4: prove the stream adapter against the live venue.

    .venv/bin/python scripts/verify_stream.py

Subscribes to real markets plus the sports feed, folds incremental updates for a
while, then re-fetches each book over REST and compares. Agreement at the touch
after hundreds of folded changes is the only evidence that matters -- the folding
rules are not something the documentation states precisely enough to trust.

What it would catch: treating a level change as a delta (depth grows without
bound), reading a change's ``price`` as the new touch, or losing updates silently
on reconnect.
"""

from __future__ import annotations

import asyncio

from deepflow.adapters.polymarket.clob import ClobMarketData
from deepflow.adapters.polymarket.discovery import SdkMarketDiscovery
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.adapters.polymarket.streams import PolymarketStreams
from deepflow.config.settings import Settings
from deepflow.core.types import ClobTokenId

FOLD_SECONDS = 25
MARKETS = 8


async def main() -> int:
    settings = Settings()
    async with PolymarketSession(settings) as session:
        discovery = SdkMarketDiscovery(session, settings)
        clob = ClobMarketData(session)
        streams = PolymarketStreams(session, settings)

        markets = await discovery.list_active_markets(limit=MARKETS)
        tokens = [o.token_id for m in markets for o in m.outcomes]
        print(f"subscribing: {len(tokens)} tokens across {len(markets)} markets\n")

        snapshots = 0
        sports_seen = 0

        async def drain_markets() -> None:
            nonlocal snapshots
            async for _ in streams.subscribe_markets(tokens):
                snapshots += 1

        async def drain_sports() -> None:
            nonlocal sports_seen
            async for _ in streams.subscribe_sports():
                sports_seen += 1

        await streams.start(tokens, sports=True)
        market_task = asyncio.create_task(drain_markets())
        sports_task = asyncio.create_task(drain_sports())

        print(f"folding for {FOLD_SECONDS}s...")
        await asyncio.sleep(FOLD_SECONDS)

        for task in (market_task, sports_task):
            task.cancel()
        await streams.stop()

        print(
            f"  connected={streams.is_connected} reconnects={streams.reconnect_count} "
            f"dropped={streams.dropped_events}"
        )
        print(f"  snapshots emitted={snapshots} sports events={sports_seen}\n")

        return await _compare(clob, streams, tokens)


async def _compare(
    clob: ClobMarketData, streams: PolymarketStreams, tokens: list[ClobTokenId]
) -> int:
    """Re-fetch each book over REST and compare against the folded state."""
    print("folded state vs a fresh REST snapshot:")
    checked = agreed = 0
    failures = 0

    for token_id in tokens:
        state = streams.book_for(token_id)
        if state is None or state.best_bid is None:
            continue

        rest = await clob.get_order_book(token_id)
        checked += 1
        match = state.best_bid == rest.best_bid and state.best_ask == rest.best_ask
        agreed += match

        flag = "MATCH" if match else "DRIFT"
        gap = " [gapped]" if state.has_gap else ""
        print(
            f"  {token_id[:12]}... folded {state.best_bid}/{state.best_ask} "
            f"rest {rest.best_bid}/{rest.best_ask}  {flag}{gap}"
        )
        print(
            f"      depth folded={len(state.bids)}x{len(state.asks)} "
            f"rest={len(rest.bids)}x{len(rest.asks)}"
        )

        if not match and not state.has_gap:
            # A mismatch with no gap flag is the real failure: state diverged and
            # nothing noticed. A flagged book is already headed for a re-anchor.
            failures += 1

    print(f"\n{agreed}/{checked} books agree at the touch")
    if failures:
        print(f"{failures} unflagged divergence(s) -- folding is wrong")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
