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
from typing import Any

from deepflow.adapters.polymarket import mapping
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.domain import Market
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId

log = get_logger(__name__)

#: The venue caps a page at 100 regardless of what is requested: asking for 500
#: returns 100, with no error and no warning. A caller that reads one page and
#: assumes it got its limit would silently see a fifth of the market universe.
MAX_PAGE_SIZE = 100


class SdkMarketDiscovery:
    """Discovery backed by the official SDK."""

    def __init__(self, session: PolymarketSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

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
