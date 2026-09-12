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

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.domain import Market
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId

log = get_logger(__name__)


class SdkMarketDiscovery:
    """Discovery backed by the official SDK."""

    def __init__(self, session: PolymarketSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    async def list_active_markets(self, *, limit: int = 500) -> Sequence[Market]:
        """Page active, order-accepting markets and normalize them.

        TODO(skeleton): ``pages = public.list_markets(closed=False, page_size=...)``
        -- a paginator, not a coroutine -- then ``await pages.first_page()`` or
        ``async for market in pages.iter_items()``. ``page.next_cursor`` is an
        opaque token to resume a later scan via ``pages.from_cursor(...)``.
        Filter to ``active and not closed and accepting_orders and
        enable_order_book``, and map via ``mapping.to_market``.

        Useful server-side filters that save paging the whole catalogue:
        ``sports_market_types=["moneyline"]`` (a win-probability model must not
        be pointed at a spread or a total), plus the tag filters from
        ``get_sports()``.
        """
        raise NotImplementedError("SdkMarketDiscovery.list_active_markets")

    async def get_market(self, condition_id: ConditionId) -> Market | None:
        raise NotImplementedError("SdkMarketDiscovery.get_market")
