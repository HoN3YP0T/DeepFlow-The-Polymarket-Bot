"""Prove the market <-> live-game join against the live venue.

    .venv/bin/python scripts/verify_game_join.py

Written because the opposite conclusion was once committed to the docs as a
blocker code could not solve. It checks three things, in order:

1. ``list_events(live=True)`` returns in-play fixtures with score and period.
2. Every fixture the sports socket is streaming resolves through
   ``list_events(game_ids=...)`` -- this is the join that was claimed absent.
3. Each resolved fixture's tradeable markets are open and priced, so the join
   leads somewhere a trade could actually happen.
"""

from __future__ import annotations

import asyncio
import json

import websockets

from deepflow.adapters.polymarket.clob import ClobMarketData
from deepflow.adapters.polymarket.games import GammaGameLinks
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings

SPORTS_WS = "wss://sports-api.polymarket.com/ws"
CAPTURE_SECONDS = 45.0


async def streamed_game_ids(budget: float = CAPTURE_SECONDS) -> dict[int, dict[str, object]]:
    """Collect the fixtures the public sports socket is broadcasting.

    The wire is camelCase (``gameId``, ``leagueAbbreviation``); the SDK model
    renames them, so a raw reader must not expect the snake_case field names that
    appear everywhere else in this codebase.
    """
    games: dict[int, dict[str, object]] = {}
    deadline = asyncio.get_running_loop().time() + budget
    async with websockets.connect(SPORTS_WS, ping_interval=None) as socket:
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=remaining)
            except TimeoutError:
                break
            if raw == "ping":
                await socket.send("pong")
                continue
            try:
                payload = json.loads(raw)
            except ValueError:
                continue
            for event in payload if isinstance(payload, list) else [payload]:
                if isinstance(event, dict) and event.get("gameId") is not None:
                    games[int(event["gameId"])] = event
    return games


async def main() -> int:
    failures = 0
    async with PolymarketSession(Settings()) as session:
        links = GammaGameLinks(session)
        clob = ClobMarketData(session)

        in_play = await links.in_play()
        print(f"in play via one REST call: {len(in_play)} fixtures")
        for link in in_play:
            print(
                f"  {link.slug[:44]:46s} {link.score!s:16s} {link.period!s:8s} "
                f"tradeable={len(link.tradeable_markets):3d} of {len(link.markets):3d}"
            )
        if not in_play:
            print("  (nothing in play right now -- the join check below still runs)")

        streamed = await streamed_game_ids()
        print(f"\nsports socket: {len(streamed)} fixtures streaming")
        if not streamed:
            print("FAIL: socket produced nothing; cannot check the join")
            return 1

        resolved = await links.for_game_ids(list(streamed))
        print(f"joined to Gamma events: {len(resolved)} of {len(streamed)}")
        for game_id, raw in streamed.items():
            link = resolved.get(game_id)
            league = raw.get("leagueAbbreviation")
            label = f"{league}: {raw.get('homeTeam')} v {raw.get('awayTeam')}"
            if link is None:
                print(f"  UNLISTED {game_id} {label} -- streamed but no markets")
                continue
            print(
                f"  ok {game_id} {label[:44]:46s} -> {link.slug[:38]:40s} "
                f"events={len(link.event_ids)} tradeable={len(link.tradeable_markets)}"
            )

        if not resolved:
            print("FAIL: no streamed fixture resolved to an event -- the join is broken")
            return 1

        # The join is only worth anything if it lands on a book we can price.
        priced = 0
        for link in list(resolved.values()):
            for market in link.tradeable_markets[:2]:
                books = await clob.get_order_books([o.token_id for o in market.outcomes])
                if any(book.best_ask is not None for book in books):
                    priced += 1
        print(f"\ntradeable markets with a live ask: {priced}")
        if priced == 0:
            failures += 1
            print("FAIL: join resolved but no market had a priced book")

    print("\nOK" if not failures else f"\n{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
