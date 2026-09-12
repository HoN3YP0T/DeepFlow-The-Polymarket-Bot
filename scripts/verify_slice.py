"""Phase 1 vertical slice: prove the data path end to end against the live venue.

Run it with no credentials -- discovery and books are public:

    .venv/bin/python scripts/verify_slice.py

What it proves, in order: the session constructs, discovery pages real markets,
the mapping survives real payloads, and the resulting book has a plausible
spread. That last check is the one that matters. The venue sends book levels
worst-price-first on both sides, so a mapping that passes the wire order through
produces a 99.8c spread while every field name still looks correct -- and nothing
else in the system would have noticed.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

from deepflow.adapters.polymarket import mapping
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.adapters.polymarket.venue import taker_fee, taker_fee_bps
from deepflow.config.settings import Settings
from deepflow.core.domain import Market, OrderBook

#: A spread wider than this on a liquid market means the book was read wrongly,
#: not that the market is illiquid.
IMPLAUSIBLE_SPREAD = Decimal("0.5")


async def main() -> int:
    settings = Settings()
    async with PolymarketSession(settings) as session:
        public = session.public

        page = await public.list_markets(closed=False).first_page()
        print(f"discovery: {len(page.items)} markets on the first page\n")

        failures = 0
        for sdk_market in page.items[:5]:
            market = mapping.to_market(sdk_market)
            if not market.outcomes:
                print(f"  {market.slug}: no open outcomes yet, skipping")
                continue

            token_id = market.outcomes[0].token_id
            book = mapping.to_order_book(await public.get_order_book(token_id=token_id))
            failures += _report(market, book)

        print("\nOK" if not failures else f"\n{failures} problem(s) found")
        return 1 if failures else 0


def _report(market: Market, book: OrderBook) -> int:
    """Print one market's state and return the number of problems found."""
    print(f"  {market.question[:58]}")
    print(
        f"    tick={market.minimum_tick_size} min_order={market.minimum_order_size} "
        f"neg_risk={market.negative_risk} delay={market.seconds_delay}s"
    )

    if book.best_bid is None or book.best_ask is None:
        print("    book: one-sided or empty")
        return 0

    spread = book.spread
    assert spread is not None
    print(
        f"    book: bid={book.best_bid} ask={book.best_ask} spread={spread} "
        f"depth={len(book.bids)}x{len(book.asks)}"
    )

    if market.fee_schedule is not None and market.fees_enabled:
        rate = market.fee_schedule.rate
        fee = taker_fee(shares=Decimal(100), price=book.best_ask, rate=rate)
        bps = taker_fee_bps(price=book.best_ask, rate=rate)
        print(f"    fee:  rate={rate} -> {fee} pUSD per 100 shares ({bps:.1f} bps)")
    else:
        print("    fee:  none (fee-free market)")

    if spread > IMPLAUSIBLE_SPREAD:
        print(f"    FAIL: spread of {spread} means the book was read in wire order")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
