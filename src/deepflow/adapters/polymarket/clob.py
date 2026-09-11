"""CLOB REST adapter -- books, midpoints, spreads, last trades.

Implements :class:`~deepflow.ports.market_data.MarketDataPort`.

Verified available on the SDK client (0.10.0): ``get_order_book``,
``get_order_books``, ``get_midpoint(s)``, ``get_price(s)``, ``get_spread(s)``,
``get_last_trade_price(s)``, ``estimate_market_price``.

REST is the cross-check, not the primary feed. The stream drives trading; these
calls prime state on startup and re-anchor it after a reconnect, because a
stream that reconnected may have missed the update that moved the book.
"""

from __future__ import annotations

from collections.abc import Sequence

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.core.domain import OrderBook, PublicTrade
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId

log = get_logger(__name__)


class ClobMarketData:
    """Point-in-time CLOB reads."""

    def __init__(self, session: PolymarketSession) -> None:
        self._session = session

    async def get_order_book(self, token_id: ClobTokenId) -> OrderBook:
        raise NotImplementedError("ClobMarketData.get_order_book")

    async def get_order_books(self, token_ids: Sequence[ClobTokenId]) -> Sequence[OrderBook]:
        """Batched. Prefer this over N single calls -- it is one round trip and
        the snapshots share a timestamp, so derived spreads stay coherent."""
        raise NotImplementedError("ClobMarketData.get_order_books")

    async def get_midpoint(self, token_id: ClobTokenId) -> object:
        raise NotImplementedError("ClobMarketData.get_midpoint")

    async def get_last_trades(
        self, token_id: ClobTokenId, *, limit: int = 50
    ) -> Sequence[PublicTrade]:
        raise NotImplementedError("ClobMarketData.get_last_trades")

    async def estimate_fill_price(
        self, token_id: ClobTokenId, *, size_shares: object
    ) -> object:
        """Walk the book for an expected fill price.

        Backs the slippage term in the EV calculation. The SDK exposes
        ``estimate_market_price`` for this; the local book walk is kept as a
        cross-check, since disagreement between the two is itself a signal that
        our book snapshot is stale.
        """
        raise NotImplementedError("ClobMarketData.estimate_fill_price")
