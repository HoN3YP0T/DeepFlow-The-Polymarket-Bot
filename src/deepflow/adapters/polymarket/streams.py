"""WebSocket adapter.

Implements :class:`~deepflow.ports.market_data.MarketStreamPort`.

Subscription specs confirmed present in ``polymarket.streams`` (0.10.0):

* ``MarketSpec``        -- book, best bid/ask, price change, last trade price,
                           tick-size change, market resolved
* ``SportsSpec``        -- live game state and results
* ``CryptoPricesSpec``  -- reference spot (backs BTC 5-minute strike distance)
* ``CryptoPricesChainlinkTwapSpec`` -- TWAP reference, where a market settles
                           against an oracle rather than spot
* ``UserSpec``          -- own order and trade updates (secure client)

Each ``subscribe_*`` is declared as a plain ``def`` returning an
``AsyncIterator``; the implementation is an async generator, which callers
consume identically with ``async for``.

Reconnection policy: exponential backoff with jitter, and an explicit gap
marker on resume. Silently resuming produces a snapshot that looks fresh while
missing the update that moved the book -- the feature engine must be told to
re-anchor from REST instead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.domain import MarketSnapshot
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId

log = get_logger(__name__)


class PolymarketStreams:
    """Managed subscriptions over the SDK's WebSocket layer."""

    def __init__(self, session: PolymarketSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._connected = False
        self._reconnects = 0

    def subscribe_markets(self, token_ids: Sequence[ClobTokenId]) -> AsyncIterator[MarketSnapshot]:
        """Stream book/price updates for the given tokens.

        TODO(skeleton): ``session.public.subscribe(MarketSpec(...))``, fold each
        event into the per-token book state, and yield a normalized snapshot.
        """
        raise NotImplementedError("PolymarketStreams.subscribe_markets")

    def subscribe_sports(self) -> AsyncIterator[object]:
        """Live game state. Feeds the per-sport probability models."""
        raise NotImplementedError("PolymarketStreams.subscribe_sports")

    def subscribe_crypto_prices(self, symbols: Sequence[str]) -> AsyncIterator[object]:
        """Reference spot prices for crypto markets."""
        raise NotImplementedError("PolymarketStreams.subscribe_crypto_prices")

    def subscribe_user(self) -> AsyncIterator[object]:
        """Own order/trade updates. Requires the secure client.

        This is the fastest path to fill confirmation, and therefore the
        primary input to resolving an uncertain submission.
        """
        raise NotImplementedError("PolymarketStreams.subscribe_user")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def reconnect_count(self) -> int:
        return self._reconnects
