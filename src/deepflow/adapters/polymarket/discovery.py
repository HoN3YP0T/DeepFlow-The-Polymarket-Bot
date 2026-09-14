"""Market discovery adapter.

Implements :class:`~deepflow.ports.market_data.MarketDiscoveryPort`.

Architectural constraint, recorded in docs/ADR-0002: the brief excludes Gamma
from the data plane, but the official SDK's discovery calls (``list_markets``,
``get_market``, ``get_event``, sports metadata, tags) are Gamma-backed
internally, and the CLOB service exposes no market-catalogue endpoint -- it
answers pricing questions about token ids you already hold. Discovery is
therefore isolated behind this adapter so the policy is a configuration
decision with one implementation to swap, not an assumption spread across the
pipeline.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from deepflow.adapters.polymarket import mapping
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import Market
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId

log = get_logger(__name__)

#: The venue caps a page at 100 regardless of what is requested: asking for 500
#: returns 100, with no error and no warning. A caller that reads one page and
#: assumes it got its limit would silently see a fifth of the market universe.
MAX_PAGE_SIZE = 100

#: The venue tag carried by every up/down event. Measured on all 71 live ones.
#:
#: The *family* tag, spanning 5m, 15m and 4h windows — it does not state the cadence, so the
#: window still has to be derived (§80). Used here to find the markets at all, which the
#: liquidity-ranked sweep never surfaces.
UP_OR_DOWN_TAG = "up-or-down"

#: How far back a window may have opened and still be worth tracking.
#:
#: Four hours, because that is the longest cadence the venue runs (§63) and such a window
#: is still live four hours after it opened.
UP_OR_DOWN_LOOKBACK = timedelta(hours=4)

#: How far ahead to pick up windows that have not opened yet.
#:
#: Must exceed the discovery interval, or a 5-minute window can open *and expire* between
#: two sweeps and never be tracked at all. More than that matters for a second reason: the
#: strike is the reference price at the opening instant (§77), so a market has to be known
#: before it opens, not when it is noticed.
UP_OR_DOWN_LOOKAHEAD = timedelta(minutes=15)


class SdkMarketDiscovery:
    """Discovery backed by the official SDK."""

    def __init__(
        self, session: PolymarketSession, settings: Settings, *, clock: Clock | None = None
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock or SystemClock()

    async def list_active_markets(self, *, limit: int = 500) -> Sequence[Market]:
        """Page active, order-accepting markets and normalize them.

        Filters are pushed to the venue wherever it supports them -- ``closed``
        and a liquidity floor -- because the alternative is paging the entire
        catalogue to discard most of it. The status flags are still re-checked
        locally: ``closed=False`` is not the same predicate as "tradeable", since
        a market can be open, undiscovered by its book, and not accepting orders.
        """
        thresholds = self._settings.thresholds
        min_liquidity = float(
            min(
                (s.min_liquidity_usdc for s in self._enabled_strategies()),
                default=0,
            )
        )

        pages = self._session.public.list_markets(
            closed=False,
            # Without this the venue omits tags entirely. They are the
            # classifier's primary signal, so discovery that does not ask for them
            # leaves it guessing from question text -- which is how a cricket
            # market gets routed to a football model.
            include_tag=True,
            liquidity_num_min=min_liquidity or None,
            page_size=min(limit, MAX_PAGE_SIZE),
        )

        markets: list[Market] = []
        skipped = 0
        async for sdk_market in pages.iter_items():
            if not self._is_tradeable(sdk_market):
                skipped += 1
                continue
            market = mapping.to_market(sdk_market)
            if not market.outcomes:
                skipped += 1
                continue
            markets.append(market)
            if len(markets) >= limit:
                break

        log.info(
            "discovery.listed",
            returned=len(markets),
            skipped=skipped,
            min_liquidity=min_liquidity,
            classification_min_confidence=str(thresholds.classification_min_confidence),
        )
        return tuple(markets)

    async def list_short_dated_crypto(self, *, limit: int = 100) -> Sequence[Market]:
        """Up/down markets, which the general sweep cannot see at all.

        Measured: `list_active_markets(limit=100)` returned 100 markets, **zero** of them
        up/down — all politics and geopolitics. These markets are listed ~24 hours before
        their window (§54) and carry no volume until the last minutes of it, so any ranking
        by liquidity buries them behind every long-horizon market on the venue. A single
        undifferentiated sweep therefore cannot serve both horizons, and the strategy that
        needs them most is the one guaranteed not to be served (§80).

        Reached through **events**, not markets, for the same structural reason the sports
        join is: the contest window lives on the event. A market fetched directly has no
        `eventStartTime`, and `contest_window_seconds()` then returns `None`, so the
        classifier cannot promote it to `BTC_5M` and the engine never sees it. Event context
        is attached to every market here for that reason.

        Filtered to markets whose book is actually open. 71 up/down events were live when
        this was written and 17 had order books; the remainder are future windows that
        cannot be traded yet, and carrying them would inflate the tracked set with markets
        no snapshot will ever arrive for.
        """
        # Bounded by the window's own start time, and that filter is load-bearing rather
        # than an optimisation. Without it the first 100 events came back with windows that
        # had **expired 39 days earlier** -- still `closed=False`, still reporting an open
        # order book -- so every market tracked was one the engine would correctly refuse,
        # and the result was indistinguishable from a broken model (§80).
        #
        # `end_date_min` does not work here: the same request filtered that way returned 100
        # events with zero current windows, because these events carry no usable end date.
        now = self._clock.now()
        events = await self._page(
            self._session.public.list_events(
                tag_slug=UP_OR_DOWN_TAG,
                closed=False,
                start_time_min=now - UP_OR_DOWN_LOOKBACK,
                start_time_max=now + UP_OR_DOWN_LOOKAHEAD,
                page_size=MAX_PAGE_SIZE,
            )
        )

        markets: list[Market] = []
        for sdk_event in events:
            for sdk_market in getattr(sdk_event, "markets", None) or ():
                if not self._is_tradeable(sdk_market):
                    continue
                market = mapping.with_event_context(mapping.to_market(sdk_market), sdk_event)
                if not market.outcomes:
                    continue
                markets.append(market)
                if len(markets) >= limit:
                    break
            if len(markets) >= limit:
                break

        log.info(
            "discovery.short_dated_crypto",
            events=len(events),
            tradeable_markets=len(markets),
            tag=UP_OR_DOWN_TAG,
        )
        return tuple(markets)

    @staticmethod
    async def _page(paginator: Any) -> tuple[Any, ...]:
        page = await paginator.first_page()
        return tuple(page.items)

    async def get_market(self, condition_id: ConditionId) -> Market | None:
        """Fetch one market by condition id.

        ``get_market`` accepts only ``id``, ``slug`` or ``url`` -- there is no
        condition-id lookup -- so this goes through ``list_markets`` with a
        ``condition_ids`` filter. The condition id is what the rest of the system
        keys on (positions, analytics, our own database), so translating here is
        cheaper than leaking the venue's Gamma id upward.
        """
        page = await self._session.public.list_markets(
            condition_ids=str(condition_id), include_tag=True
        ).first_page()
        for sdk_market in page.items:
            if str(sdk_market.condition_id) != str(condition_id):
                continue
            market = mapping.to_market(sdk_market)
            return market if market.outcomes else None
        return None

    def _enabled_strategies(self) -> Sequence[Any]:
        """Strategy threshold blocks that are switched on."""
        sports = self._settings.thresholds.sports
        return tuple(
            s
            for s in (sports.football, sports.cricket, sports.tennis, sports.badminton)
            if s.enabled
        )

    @staticmethod
    def _is_tradeable(sdk_market: Any) -> bool:
        """Whether the venue would accept an order on this market right now.

        All four flags are required. ``active and not closed`` says the market
        exists and has not settled; ``accepting_orders`` and ``enable_order_book``
        say there is somewhere for an order to go. A market can satisfy the first
        pair and not the second -- it is listed but its book has not opened -- and
        pricing it would mean quoting off a book that does not exist.
        """
        state = sdk_market.state
        return bool(
            state.active and not state.closed and state.accepting_orders and state.enable_order_book
        )
