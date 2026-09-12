"""WebSocket adapter.

Implements :class:`~deepflow.ports.market_data.MarketStreamPort`.

Subscription specs in ``polymarket.streams``:

* ``MarketSpec(token_ids=[...])`` -- ``book``, ``price_change``,
  ``best_bid_ask``, ``last_trade_price``, ``tick_size_change``, ``new_market``,
  ``market_resolved``
* ``SportsSpec()``       -- live game state. Takes no filter: the feed sends
  *every* game, so filtering to our markets is our job.
* ``CryptoPricesSpec(topic=..., symbols=[...])`` -- one spec per source, with
  source-specific symbol formats: Binance wants ``btcusdt``, Chainlink wants
  ``btc/usd``. Topics ``prices.crypto.binance`` and ``prices.crypto.chainlink``.
* ``CryptoPricesChainlinkTwapSpec(window_seconds=30|60)`` -- the TWAP a crypto
  market may actually settle against. Only 30 and 60 second windows exist.
* ``EquityPricesSpec`` / ``CommentsSpec`` -- unused here.
* ``UserSpec()`` -- own order and trade updates (secure client); accepts
  condition ids to narrow the feed.

The SDK multiplexes: ``await client.subscribe([...])`` returns one async context
manager yielding a merged stream, discriminated on ``event.topic`` then
``event.type``. The per-feed methods below are our own fan-out over that single
stream, not separate connections -- opening one socket per feed would multiply
the reconnect surface for no benefit.

Folding book updates correctly (the part that is easy to get wrong):

* ``book`` is a full snapshot and carries ``hash``, ``min_order_size``,
  ``tick_size`` and ``neg_risk``. Those last three are trading constraints and
  must update the stored market, not just the book.
* ``price_change`` carries ``price``, ``size``, ``side`` per changed level and is
  a **level replacement, not a delta** -- ``size`` is the level's new total.
  Adding it to the existing size silently inflates depth, which inflates every
  slippage estimate derived from it. A ``size`` of zero removes the level.
* ``tick_size_change`` must overwrite any cached tick size immediately. Orders
  priced on a stale grid are rejected outright.

Heartbeats differ per socket and are not interchangeable (see
:mod:`deepflow.adapters.polymarket.venue`): the market socket expects us to send
``PING`` every 10s, RTDS every 5s, while the sports socket *sends* lowercase
``ping`` every 5s and closes the connection if ``pong`` does not follow within
10s.

Reconnection policy: exponential backoff with jitter, and an explicit gap
marker on resume. Silently resuming produces a snapshot that looks fresh while
missing the update that moved the book -- the feature engine must be told to
re-anchor from REST instead. The ``hash`` on ``book`` and ``price_change`` is
the cheap cross-check that resume actually worked.
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
        """Live game state.

        Yields :class:`~deepflow.adapters.polymarket.sports_feed.SportsFeedEvent`.
        The payload is far thinner than the per-sport ``GameState`` models
        expect -- score string, period, elapsed, status, and possession for NFL
        and CFB -- and covers NFL, NHL, MLB, NBA, CBB, CFB, Soccer, Esports and
        Tennis only. See :mod:`deepflow.adapters.polymarket.sports_feed` for what
        that rules out.
        """
        raise NotImplementedError("PolymarketStreams.subscribe_sports")

    def subscribe_crypto_prices(
        self, symbols: Sequence[str], *, source: str = "binance"
    ) -> AsyncIterator[object]:
        """Reference prices for crypto markets.

        ``source`` selects the topic and therefore the symbol format, and the
        choice is not cosmetic: a market settling against a Chainlink TWAP
        priced off Binance spot is mispriced by the basis between them, and at
        the short horizons this system trades that basis is the whole edge.
        Which feed a market settles against is read from its resolution
        criteria; a mismatch is an abstention, not an approximation.
        """
        raise NotImplementedError("PolymarketStreams.subscribe_crypto_prices")

    def subscribe_crypto_twap(
        self, symbols: Sequence[str], *, window_seconds: int = 30
    ) -> AsyncIterator[object]:
        """Chainlink-computed TWAP prices. Windows of 30 or 60 seconds only."""
        raise NotImplementedError("PolymarketStreams.subscribe_crypto_twap")

    def subscribe_user(self) -> AsyncIterator[object]:
        """Own order/trade updates. Requires the secure client.

        This is the fastest path to fill confirmation, and therefore the
        primary input to resolving an uncertain submission.

        Two shapes arrive on this topic. ``order`` events carry
        ``order_event_type`` (PLACEMENT / UPDATE / CANCELLATION) and a status of
        LIVE / MATCHED / DELAYED / UNMATCHED / CANCELED. ``trade`` events carry a
        *settlement* status -- MATCHED, MATCHED_NOT_BROADCASTED, MINED,
        CONFIRMED, RETRYING, FAILED.

        A trade is not final at MATCHED. Booking the position there and never
        following the transition to CONFIRMED (or FAILED) is how a phantom
        position enters the book through the path that was supposed to be the
        authoritative one. Only CONFIRMED settles; FAILED must raise
        ``TradeSettlementFailedError`` and trip SETTLEMENT_FAILURE.
        """
        raise NotImplementedError("PolymarketStreams.subscribe_user")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def reconnect_count(self) -> int:
        return self._reconnects
