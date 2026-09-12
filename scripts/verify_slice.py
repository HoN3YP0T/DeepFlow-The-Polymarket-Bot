"""Phase 1 vertical slice: prove the data path end to end against the live venue.

Run it with no credentials -- discovery and books are public:

    .venv/bin/python scripts/verify_slice.py

What it proves, in order: the session constructs, discovery pages and filters
real markets, the mapping survives real payloads, batched books stay attached to
the right token, and spreads come out plausible.

Two of those checks exist because of bugs the venue makes easy and nothing else
would catch:

* The venue sends book levels worst-price-first on both sides, so a mapping that
  passes the wire order through produces a 99.8c spread while every field name
  still looks correct.
* ``get_order_books`` returns results in an arbitrary order. Zipping positionally
  attributes a binary market's NO book to its YES token, and since the two are
  complements the result is a plausible price that happens to be the reflection
  of the truth. The complement check below is the cheap detector: two best asks
  on one market must sum to roughly 1.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from decimal import Decimal

from deepflow.adapters.polymarket.clob import ClobMarketData
from deepflow.adapters.polymarket.discovery import SdkMarketDiscovery
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.adapters.polymarket.venue import taker_fee, taker_fee_bps
from deepflow.config.settings import Settings
from deepflow.core.domain import Market, OrderBook

#: A spread wider than this on a liquid market means the book was read wrongly,
#: not that the market is illiquid.
IMPLAUSIBLE_SPREAD = Decimal("0.5")

#: A binary market's two best asks sum to slightly over 1 -- the excess is the
#: spread. Outside this band, the books are not the books we asked for.
COMPLEMENT_BAND = (Decimal("0.98"), Decimal("1.05"))


async def main() -> int:
    settings = Settings()
    async with PolymarketSession(settings) as session:
        discovery = SdkMarketDiscovery(session, settings)
        clob = ClobMarketData(session)

        markets = await discovery.list_active_markets(limit=5)
        print(f"discovery: {len(markets)} tradeable markets\n")

        failures = 0
        for market in markets:
            tokens = [outcome.token_id for outcome in market.outcomes]
            books = await clob.get_order_books(tokens)

            # Batched, so this also exercises the re-projection: books must come
            # back attached to the token they were requested for.
            for token_id, book in zip(tokens, books, strict=True):
                if book.token_id != token_id:
                    print(f"  FAIL: book for {token_id} came back as {book.token_id}")
                    failures += 1

            failures += _report(market, books)

        print("\nOK" if not failures else f"\n{failures} problem(s) found")
        return 1 if failures else 0


def _report(market: Market, books: Sequence[OrderBook]) -> int:
    """Print one market's state and return the number of problems found."""
    print(f"  {market.question[:58]}")
    print(
        f"    tick={market.minimum_tick_size} min_order={market.minimum_order_size} "
        f"neg_risk={market.negative_risk} delay={market.seconds_delay}s"
    )

    failures = 0
    for outcome, book in zip(market.outcomes, books, strict=True):
        if book.best_bid is None or book.best_ask is None:
            print(f"    {outcome.label}: one-sided or empty book")
            continue

        spread = book.spread
        assert spread is not None
        print(
            f"    {outcome.label}: bid={book.best_bid} ask={book.best_ask} "
            f"spread={spread} depth={len(book.bids)}x{len(book.asks)}"
        )
        if spread > IMPLAUSIBLE_SPREAD:
            print(f"      FAIL: spread of {spread} means the book was read in wire order")
            failures += 1

    failures += _check_complement(books)

    if market.fee_schedule is not None and market.fees_enabled and books[0].best_ask:
        rate = market.fee_schedule.rate
        price = books[0].best_ask
        fee = taker_fee(shares=Decimal(100), price=price, rate=rate)
        bps = taker_fee_bps(price=price, rate=rate)
        print(f"    fee:  rate={rate} -> {fee} pUSD per 100 shares ({bps:.1f} bps)")
    else:
        print("    fee:  none (fee-free market)")

    return failures


def _check_complement(books: Sequence[OrderBook]) -> int:
    """A binary market's two best asks must sum to roughly 1.

    This is the detector for a swapped batch: YES and NO are complements, so a
    misattributed book still looks like a valid price. Only the sum gives it away.
    """
    if len(books) != 2:
        return 0
    asks = [book.best_ask for book in books]
    if any(ask is None for ask in asks):
        return 0

    total = sum(ask for ask in asks if ask is not None)
    low, high = COMPLEMENT_BAND
    if not low < total < high:
        print(f"      FAIL: YES+NO best asks sum to {total}, expected ~1.00")
        return 1
    print(f"    complement: {total} (expected ~1.00)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
