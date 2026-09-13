"""Market discovery, pricing and streaming ports."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Protocol, runtime_checkable

from deepflow.core.domain import Market, MarketSnapshot, OrderBook, PublicTrade
from deepflow.core.types import ClobTokenId, ConditionId


@runtime_checkable
class MarketDiscoveryPort(Protocol):
    """Finds and normalizes tradeable markets.

    Deliberately separate from :class:`MarketDataPort`: discovery metadata and
    live pricing come from different Polymarket services with different
    freshness and availability characteristics, and the brief constrains which
    services may be used for which purpose.
    """

    async def list_active_markets(self, *, limit: int = 500) -> Sequence[Market]:
        """Active, order-accepting markets."""
        ...

    async def get_market(self, condition_id: ConditionId) -> Market | None: ...


@runtime_checkable
class MarketDataPort(Protocol):
    """Point-in-time CLOB reads. Used to prime state and to cross-check the
    stream -- polling alone is too slow to trade on."""

    async def get_order_book(self, token_id: ClobTokenId) -> OrderBook: ...

    async def get_order_books(self, token_ids: Sequence[ClobTokenId]) -> Sequence[OrderBook]: ...

    async def get_midpoint(self, token_id: ClobTokenId) -> object: ...

    async def get_last_trades(
        self, token_id: ClobTokenId, *, limit: int = 50
    ) -> Sequence[PublicTrade]: ...


@runtime_checkable
class MarketStreamPort(Protocol):
    """Real-time push feed.

    Implementations own reconnection with backoff, and must surface a gap
    rather than silently resuming: a resumed stream with a hole in it produces
    a snapshot that looks fresh and is not.
    """

    def subscribe_markets(
        self, token_ids: Sequence[ClobTokenId]
    ) -> AsyncIterator[MarketSnapshot]: ...

    def subscribe_sports(self) -> AsyncIterator[object]:
        """Live game-state events for sports markets."""
        ...

    def subscribe_crypto_prices(self, symbols: Sequence[str]) -> AsyncIterator[object]:
        """Reference spot feed. Backs the BTC 5-minute strike distance."""
        ...

    @property
    def is_connected(self) -> bool: ...

    @property
    def reconnect_count(self) -> int: ...
