"""Prove short-dated crypto markets exist and classify correctly.

    .venv/bin/python scripts/verify_short_dated_crypto.py

`docs/STATUS.md` recorded "no short-dated crypto markets" as a blocker, on a scan
that measured each market's window as ``end_date - start_date``. These markets open
about a day before the five minutes they settle on, so that subtraction returns
~86,200s for a 300s contest and every one of them read as a long-horizon forecast.

Three things checked here, in order: the markets exist and are accepting orders; the
contest window measures 300s rather than a day; and the classifier reaches `BTC_5M`
rather than plain `CRYPTO`.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from deepflow.adapters.polymarket import mapping
from deepflow.adapters.polymarket.clob import ClobMarketData
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.enums import MarketCategory
from deepflow.core.errors import PolymarketApiError
from deepflow.pipeline.classifier import MarketClassifier

#: The venue's own cadence family. Searching the title rather than guessing slugs,
#: because the slug carries a unix timestamp that changes every five minutes.
TITLE_SEARCH = "Up or Down"

#: How far either side of now a window has to start to be worth checking.
#:
#: ``closed=False`` is not enough on its own: the first page of results came back
#: full of windows from months ago, still open and long past, accepting no orders and
#: carrying no book. Those classify correctly and prove nothing about tradeability,
#: so the check targets the windows a bot would actually be looking at.
RELEVANT_WINDOW_SECONDS = 15 * 60


async def main() -> int:
    failures = 0
    settings = Settings()

    async with PolymarketSession(settings) as session:
        now = datetime.now(UTC)
        page = await session.public.list_events(
            closed=False,
            title_search=TITLE_SEARCH,
            start_time_min=now - timedelta(seconds=RELEVANT_WINDOW_SECONDS),
            start_time_max=now + timedelta(seconds=RELEVANT_WINDOW_SECONDS),
        ).first_page()
        events = [event for event in page.items if "updown" in (event.slug or "")]
        print(f"short-dated crypto windows within ±15 min of now: {len(events)}")
        if not events:
            print("FAIL: none current -- rerun; the venue lists one per asset per 5 minutes")
            return 1

        clob = ClobMarketData(session)
        classifier = MarketClassifier(settings.thresholds)

        for event in events[:8]:
            for sdk_market in event.markets:
                market = mapping.with_event_context(mapping.to_market(sdk_market), event)
                window = market.contest_window_seconds()
                verdict = classifier.classify(market)

                listing = None
                if market.start_date is not None and market.end_date is not None:
                    listing = int((market.end_date - market.start_date).total_seconds())

                # A window that has not started yet is listed with no book at all,
                # and `get_order_books` raises rather than returning a short list --
                # correct for sports, where a missing book risks mis-attribution, and
                # simply "not open yet" here. Absence is reported, not treated as a
                # classification failure.
                ask: object = "no book"
                if market.outcomes:
                    try:
                        books = await clob.get_order_books([o.token_id for o in market.outcomes])
                    except PolymarketApiError:
                        pass
                    else:
                        ask = next((b.best_ask for b in books if b.best_ask is not None), None)

                ok = verdict.category is MarketCategory.BTC_5M and window == 300.0
                failures += 0 if ok else 1
                print(
                    f"  {'ok  ' if ok else 'FAIL'} {str(event.slug)[:28]:30s} "
                    f"contest={window!s:7s} listing={listing!s:7s} "
                    f"{verdict.category.value:8s} ask={ask!s:7s} "
                    f"accepting={market.accepting_orders}"
                )

    print("\nOK" if not failures else f"\n{failures} market(s) misread")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
