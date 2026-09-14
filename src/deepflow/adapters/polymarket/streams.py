"""WebSocket adapter.

Implements :class:`~deepflow.ports.market_data.MarketStreamPort`.

The SDK multiplexes: ``await client.subscribe([...])`` opens **one** subscription
and yields a single merged stream, discriminated on ``event.topic`` then
``event.type``. The port declares a method per feed. This adapter reconciles the
two by pumping the merged stream once and fanning events out into per-feed
queues, so there is one connection, one reconnect path and one heartbeat to get
right rather than three of each.

Note that ``subscribe`` is an async function despite what its return annotation
suggests -- it must be awaited to get the handle, which is then an async context
manager::

    async with await client.subscribe([MarketSpec(token_ids=[...])]) as stream:
        async for event in stream:
            ...

Observed event shapes (live, 2026-09-12):

* ``book`` -- full snapshot. Carries ``tick_size``, but ``min_order_size`` and
  ``neg_risk`` come back **None** on the stream even though REST populates them.
  Trading constraints therefore come from discovery; only the tick size can be
  refreshed from here.
* ``price_change`` -- one event carries a *list* of changes, each with its own
  ``asset_id``, so both sides of a market can move in a single frame. Folding
  semantics and the traps in them are documented in :mod:`book_state`.
* ``tick_size_change`` -- must overwrite any cached tick size immediately; orders
  priced on a stale grid are rejected outright.
* ``market_resolved`` -- terminal for that market.
* ``sport_result`` -- see :mod:`sports_feed`. The feed sends **every** game, with
  no server-side filter, so narrowing to our markets is our job.

Backpressure: queues are bounded and **drop the oldest event** when full, per
docs/ARCHITECTURE.md. A slow consumer must not stall market-data ingest, and for
book data the newest frame is the valuable one. A drop marks the affected books
as gapped rather than passing silently, because a dropped ``price_change`` is
exactly the hole that makes a book look fresh while being wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import random
from collections.abc import AsyncGenerator, AsyncIterator, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final, Literal

from deepflow.adapters.polymarket.book_state import BookState
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.adapters.polymarket.sports_feed import SportsFeedEvent
from deepflow.config.settings import Settings
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import MarketSnapshot, Microstructure, OrderBook, ReferencePrice
from deepflow.core.enums import DataQuality
from deepflow.core.errors import ConfigurationError
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId, ConditionId

log = get_logger(__name__)

#: Source tag for the Chainlink TWAP series.
#:
#: Must match ``Btc5mEngine.REQUIRED_SOURCE``: the engine refuses to price against any
#: other series, because a market settling on a TWAP priced off spot is wrong by the basis
#: between them and that basis is the whole edge at a five-minute horizon (§63).
TWAP_SOURCE: Final = "chainlink_twap"

#: The averaging windows the venue publishes. 30 for 5-minute markets, 60 for the
#: 15-minute and 4-hour variants (§63); the SDK rejects anything else, and typing it here
#: makes that a compile-time constraint rather than a runtime surprise.
TwapWindow = Literal[30, 60]

#: Spot topics, by the source name callers use. Two exist; there is no third.
SpotTopic = Literal["prices.crypto.binance", "prices.crypto.chainlink"]
SPOT_TOPICS: Final[dict[str, SpotTopic]] = {
    "binance": "prices.crypto.binance",
    "chainlink": "prices.crypto.chainlink",
}

#: Per-feed queue depth. Deep enough to ride out a GC pause or a slow database
#: write, shallow enough that a wedged consumer is noticed in seconds rather than
#: consuming memory until the process dies.
QUEUE_MAXSIZE = 1000

#: Events whose ``type`` we fold into book state.
_MARKET_TYPES = frozenset(
    {"book", "price_change", "tick_size_change", "best_bid_ask", "last_trade_price"}
)


def _ask_side_notional(books: Sequence[OrderBook]) -> Decimal | None:
    """Total notional resting on the ask side across a market's books.

    ``None`` when no book has an ask at all, because zero depth and unmeasured depth are
    different facts: a market with no offers cannot be bought, and one we cannot see is one
    we must not trade. Both block an entry; only the first is the market's own state.
    """
    total = Decimal(0)
    measured = False
    for book in books:
        for level in book.asks:
            total += level.price * level.size
            measured = True
    return total if measured else None


def _symbol_filter(symbols: Sequence[str]) -> list[str] | None:
    """The SDK's symbol filter, where **empty and absent are different things**.

    ``None`` subscribes to every symbol the venue publishes; an empty list is rejected
    outright with "symbols must be non-empty when provided". Passing ``[]`` through cost a
    live run: the reference feed raised on every attempt, retried every two seconds, and
    each retry recorded a reconnect until the websocket breaker latched — so the symptom
    was a tripped breaker and a model that never spoke, with nothing pointing at an empty
    list as the cause.
    """
    filtered = [symbol.lower() for symbol in symbols if symbol.strip()]
    return filtered or None


def _to_twap_reference(event: Any) -> ReferencePrice | None:
    """One Chainlink TWAP event as a :class:`ReferencePrice`.

    ``value`` arrives as ``full_accuracy_value`` in Chainlink's 18-decimal fixed point and
    the SDK has already scaled it; ``timestamp`` is epoch **milliseconds** on the payload,
    unlike the event envelope's parsed datetime. Read from the payload because the
    envelope's timestamp is when the message was published, and what the model needs is
    the instant the average refers to.
    """
    payload = getattr(event, "payload", None)
    if payload is None:
        return None
    try:
        return ReferencePrice(
            symbol=str(payload.symbol),
            value=Decimal(str(payload.value)),
            source=TWAP_SOURCE,
            window_seconds=int(payload.window_seconds),
            observed_at=datetime.fromtimestamp(int(payload.timestamp) / 1000, UTC),
        )
    except (AttributeError, TypeError, ValueError, ArithmeticError) as exc:
        log.warning("streams.twap_unparsed", error=f"{type(exc).__name__}: {exc}")
        return None


def _to_spot_reference(event: Any, *, source: str) -> ReferencePrice | None:
    """One spot price event as a :class:`ReferencePrice`, tagged with its own source.

    ``window_seconds`` stays ``None``: a spot tick is not a TWAP with a zero-length
    window, it is a different quantity, and the engine's source check depends on the two
    never being conflated.
    """
    payload = getattr(event, "payload", None)
    if payload is None:
        return None
    try:
        stamp = int(getattr(payload, "timestamp", 0))
        return ReferencePrice(
            symbol=str(payload.symbol),
            value=Decimal(str(payload.value)),
            source=source,
            window_seconds=None,
            observed_at=datetime.fromtimestamp(stamp / 1000, UTC),
        )
    except (AttributeError, TypeError, ValueError, ArithmeticError) as exc:
        log.warning("streams.spot_price_unparsed", error=f"{type(exc).__name__}: {exc}")
        return None


class PolymarketStreams:
    """Managed subscriptions over the SDK's WebSocket layer."""

    def __init__(
        self,
        session: PolymarketSession,
        settings: Settings,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._session = session
        self._settings = settings
        self._clock = clock or SystemClock()

        self._connected = False
        self._reconnects = 0
        self._pump: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

        # When the connection last delivered anything. This -- not each book's own
        # last change -- is the freshness signal for every folded book; see
        # BookState.snapshot. It stops advancing on a disconnect, so the books age
        # out into STALE on their own with no extra bookkeeping.
        self._last_event_at: datetime | None = None

        self._books: dict[ClobTokenId, BookState] = {}
        self._token_to_condition: dict[ClobTokenId, ConditionId] = {}
        self._market_queue: asyncio.Queue[ConditionId] = asyncio.Queue(QUEUE_MAXSIZE)
        self._sports_queue: asyncio.Queue[SportsFeedEvent] = asyncio.Queue(QUEUE_MAXSIZE)
        self._dropped = 0

    # --- Lifecycle --------------------------------------------------------
    async def start(self, token_ids: Sequence[ClobTokenId], *, sports: bool = False) -> None:
        """Open the subscription and start pumping events.

        Idempotent. The caller supplies the full token set up front because the
        SDK subscribes per connection: changing the set means reopening, and
        doing that implicitly on every new market would churn the socket.
        """
        if self._pump is not None:
            return
        self._stop.clear()
        self._pump = asyncio.create_task(self._run(list(token_ids), sports=sports))

    async def stop(self) -> None:
        """Stop pumping and close the subscription."""
        self._stop.set()
        if self._pump is not None:
            self._pump.cancel()
            # Shutdown must not fail. Anything the pump raises on the way down --
            # a cancellation, or a socket error mid-teardown -- is noise at this
            # point, and letting it escape would abort the ordered shutdown that
            # keeps an in-flight order from becoming an uncertain one.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._pump
            self._pump = None
        self._connected = False

    async def _run(self, token_ids: list[ClobTokenId], *, sports: bool) -> None:
        """Connect, pump, and reconnect with backoff until stopped."""
        from polymarket.streams import MarketSpec, SportsSpec

        delay = self._settings.polymarket.ws_reconnect_base_delay_seconds
        max_delay = self._settings.polymarket.ws_reconnect_max_delay_seconds

        while not self._stop.is_set():
            specs: list[Any] = [MarketSpec(token_ids=list(token_ids))]
            if sports:
                specs.append(SportsSpec())

            try:
                async with await self._session.public.subscribe(specs) as stream:
                    self._connected = True
                    delay = self._settings.polymarket.ws_reconnect_base_delay_seconds
                    log.info("streams.connected", tokens=len(token_ids), sports=sports)
                    async for event in stream:
                        if self._stop.is_set():
                            break
                        self._handle(event)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("streams.disconnected", exc_info=True)

            self._connected = False
            if self._stop.is_set():
                return

            # Every book is now suspect. The stream may have dropped updates
            # while we were away, and a resumed stream says nothing about what
            # was missed -- so mark the gap before reconnecting, not after, or a
            # consumer racing the reconnect sees state it believes is continuous.
            self._mark_all_gapped()
            self._reconnects += 1

            # Jitter is not decoration: without it every client that dropped on
            # the same venue restart reconnects in lockstep and hammers the
            # socket at the same instant.
            wait = min(delay, max_delay) * (1 + random.random() * 0.5)  # noqa: S311
            log.warning("streams.reconnecting", attempt=self._reconnects, delay=round(wait, 2))
            await asyncio.sleep(wait)
            delay = min(delay * 2, max_delay)

    # --- Event handling ---------------------------------------------------
    def _handle(self, event: Any) -> None:
        # Any event proves the socket is alive, including one for a topic we do not
        # act on. Liveness belongs to the connection, not to whichever market
        # happened to move.
        self._last_event_at = self._clock.now()

        topic = getattr(event, "topic", None)
        if topic == "market":
            self._handle_market(event)
        elif topic == "sports":
            self._offer(self._sports_queue, self._to_sports_event(event))

    def _handle_market(self, event: Any) -> None:
        kind = getattr(event, "type", None)
        if kind not in _MARKET_TYPES and kind != "market_resolved":
            return

        payload = event.payload
        timestamp = self._timestamp(payload)

        if kind == "book":
            token_id = ClobTokenId(str(payload.asset_id))
            state = self._state_for(token_id, payload)
            state.apply_snapshot(
                bids=[(level.price, level.size) for level in payload.bids],
                asks=[(level.price, level.size) for level in payload.asks],
                timestamp=timestamp,
                tick_size=getattr(payload, "tick_size", None),
            )
            self._publish(token_id)
            return

        if kind == "price_change":
            # One frame can carry changes for several tokens, including both
            # sides of the same market.
            touched: set[ClobTokenId] = set()
            for change in payload.price_changes:
                token_id = ClobTokenId(str(change.asset_id))
                known = self._books.get(token_id)
                if known is None:
                    # No snapshot yet: applying a level change to an absent book
                    # would build a book out of fragments and present it as real.
                    continue
                known.apply_level(
                    side=str(change.side),
                    price=change.price,
                    size=change.size,
                    timestamp=timestamp,
                    best_bid=getattr(change, "best_bid", None),
                    best_ask=getattr(change, "best_ask", None),
                )
                touched.add(token_id)

            for token_id in touched:
                if self._books[token_id].drifted():
                    log.warning("streams.book_drift", token_id=token_id)
                    self._books[token_id].mark_gap()
                self._publish(token_id)
            return

        if kind == "tick_size_change":
            token_id = ClobTokenId(str(payload.asset_id))
            known = self._books.get(token_id)
            new_tick = getattr(payload, "new_tick_size", None) or getattr(
                payload, "tick_size", None
            )
            if known is not None and new_tick is not None:
                known.tick_size = Decimal(str(new_tick))
                log.info("streams.tick_size_changed", token_id=token_id, tick=str(new_tick))
            return

        if kind == "market_resolved":
            log.info("streams.market_resolved", market=str(getattr(payload, "market", "")))

    def _state_for(self, token_id: ClobTokenId, payload: Any) -> BookState:
        state = self._books.get(token_id)
        if state is None:
            state = BookState(token_id=token_id)
            self._books[token_id] = state
        condition_id = getattr(payload, "condition_id", None) or getattr(payload, "market", None)
        if condition_id:
            self._token_to_condition[token_id] = ConditionId(str(condition_id))
        return state

    def _publish(self, token_id: ClobTokenId) -> None:
        condition_id = self._token_to_condition.get(token_id)
        if condition_id is not None:
            self._offer(self._market_queue, condition_id)

    def _offer(self, queue: asyncio.Queue[Any], item: Any) -> None:
        """Enqueue, dropping the oldest item when full.

        Dropping beats blocking: the pump is the only reader of the socket, so a
        blocked put stalls every feed behind one slow consumer. For book data the
        newest frame is also the most valuable, which makes the oldest the right
        thing to lose.
        """
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            self._dropped += 1
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - racing a consumer
                pass
            else:
                queue.task_done()
            # A dropped update is a hole in the book, so say so rather than
            # letting the next snapshot look continuous.
            self._mark_all_gapped()
            log.warning("streams.queue_overflow", dropped=self._dropped)
            # Still full means the consumer is wedged, not merely slow. Losing
            # this event too is the correct outcome: the alternative is blocking
            # the only reader of the socket.
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(item)

    def _mark_all_gapped(self) -> None:
        for state in self._books.values():
            state.mark_gap()

    def _timestamp(self, payload: Any) -> datetime:
        raw = getattr(payload, "timestamp", None)
        return raw if isinstance(raw, datetime) else self._clock.now()

    @staticmethod
    def _to_sports_event(event: Any) -> SportsFeedEvent:
        from deepflow.adapters.polymarket import mapping

        return mapping.to_sports_feed_event(event)

    # --- Port surface -----------------------------------------------------
    def subscribe_markets(
        self, token_ids: Sequence[ClobTokenId]
    ) -> AsyncGenerator[MarketSnapshot, None]:
        """Stream normalized snapshots for the given tokens.

        Yields one :class:`MarketSnapshot` per updated market, carrying every book
        we hold for that market's tokens -- both sides of a binary, so a consumer
        can check the complement without a second call.

        ``quality`` is ``DEGRADED`` while any contributing book is flagged gapped.
        Degraded state is explicitly usable for monitoring and exits but blocks
        new entries: reducing an uncertain position shrinks the problem, opening
        one on stale depth compounds it.

        Typed as an ``AsyncGenerator`` rather than an ``AsyncIterator`` because the
        caller needs ``aclose()``: the token set changes as markets are discovered, and
        a subscription abandoned mid-iteration leaves its consumer alive to race the
        replacement for events.
        """
        return self._market_snapshots(token_ids)

    async def _market_snapshots(
        self, token_ids: Sequence[ClobTokenId]
    ) -> AsyncGenerator[MarketSnapshot, None]:
        await self.start(token_ids)
        watched = {ClobTokenId(str(t)) for t in token_ids}

        while not self._stop.is_set():
            condition_id = await self._market_queue.get()
            try:
                snapshot = self._snapshot_for(condition_id, watched)
                if snapshot is not None:
                    yield snapshot
            finally:
                self._market_queue.task_done()

    def _snapshot_for(
        self, condition_id: ConditionId, watched: set[ClobTokenId]
    ) -> MarketSnapshot | None:
        tokens = [
            token
            for token, cond in self._token_to_condition.items()
            if cond == condition_id and token in watched
        ]
        books = []
        gapped = False
        for token in tokens:
            state = self._books[token]
            book = state.snapshot(as_of=self._last_event_at)
            if book is None:
                continue
            books.append(book)
            gapped = gapped or state.has_gap

        if not books:
            return None

        return MarketSnapshot(
            condition_id=condition_id,
            books=tuple(books),
            microstructure=Microstructure(),
            # Book depth, and named as such: the notional resting on the side we would buy
            # from. **Not** Gamma's `liquidity` metric, which is a different aggregate the
            # stream does not carry — and because it does not, this field was `None` on
            # every one of 59,048 recorded snapshots, so the gate's liquidity check could
            # never pass from streamed data. Depth is also the better answer to the question
            # that check asks: whether there is enough here to trade against now.
            liquidity=_ask_side_notional(books),
            captured_at=max(book.captured_at for book in books),
            quality=DataQuality.DEGRADED if gapped else DataQuality.FRESH,
        )

    async def subscribe_sports(self) -> AsyncIterator[SportsFeedEvent]:
        """Live game state.

        The venue streams every game with no server-side filter, so callers must
        resolve each ``game_id`` themselves. Do **not** match it against
        ``Market.game_id``: that field is a different id space (the child contest,
        not the fixture) and is empty on fixture-level markets. The join is on the
        event -- :class:`deepflow.adapters.polymarket.games.GammaGameLinks`. This
        docstring previously said the opposite, and believing it is how the join
        came to be recorded as impossible.

        See :mod:`sports_feed` for how little the payload carries, and note that
        the SDK model drops the wire's ``eventState`` block entirely.
        """
        while not self._stop.is_set():
            event = await self._sports_queue.get()
            try:
                yield event
            finally:
                self._sports_queue.task_done()

    async def subscribe_crypto_twap(
        self, symbols: Sequence[str], *, window_seconds: TwapWindow = 30
    ) -> AsyncIterator[ReferencePrice]:
        """Chainlink-computed TWAP prices -- what crypto up/down markets settle on.

        Windows of 30 or 60 seconds only; the SDK rejects anything else. 5-minute markets
        use 30 and the 15-minute and 4-hour variants use 60 (§63).

        Symbols are lowercase slash-delimited pairs (``btc/usd``), **not** Binance's
        ``btcusdt``. Passed through unchanged so a wrong format fails loudly at the venue
        rather than silently subscribing to nothing.

        Its own subscription rather than a topic on the market pump: it is per-symbol and
        continuous, independent of which markets are being tracked, and the reference
        series must survive a market-set change that reopens the book socket. A gap in it
        is what makes the model abstain, so keeping it out of that churn is the point.
        """
        from polymarket.streams import CryptoPricesChainlinkTwapSpec

        handle = await self._session.public.subscribe(
            CryptoPricesChainlinkTwapSpec(
                window_seconds=window_seconds, symbols=_symbol_filter(symbols)
            )
        )
        try:
            async for event in handle:
                price = _to_twap_reference(event)
                if price is not None:
                    self._last_event_at = self._clock.now()
                    yield price
        finally:
            await handle.close()

    async def subscribe_crypto_prices(
        self, symbols: Sequence[str], *, source: str = "binance"
    ) -> AsyncIterator[ReferencePrice]:
        """Spot reference prices, for monitoring and for measuring the basis.

        **Not** what an up/down market settles against, and deliberately tagged with its
        own source so it cannot be mistaken for one: a market settling on a Chainlink TWAP
        priced off Binance spot is mispriced by the basis between them, and at these
        horizons that basis is the whole edge. ``Btc5mEngine`` refuses a series whose
        source is not the TWAP for exactly this reason.

        ``source`` selects the topic and therefore the symbol format -- ``btcusdt`` for
        Binance, ``btc/usd`` for Chainlink -- and the two are not interchangeable. Only
        ``binance`` and ``chainlink`` exist as spot topics; anything else is refused here
        rather than sent, since the SDK's own error names a topic string the caller never
        supplied.
        """
        from polymarket.streams import CryptoPricesSpec

        topic = SPOT_TOPICS.get(source)
        if topic is None:
            raise ConfigurationError(
                f"unknown crypto price source {source!r}: expected one of {sorted(SPOT_TOPICS)}"
            )
        handle = await self._session.public.subscribe(
            CryptoPricesSpec(topic=topic, symbols=_symbol_filter(symbols))
        )
        try:
            async for event in handle:
                price = _to_spot_reference(event, source=source)
                if price is not None:
                    self._last_event_at = self._clock.now()
                    yield price
        finally:
            await handle.close()

    def subscribe_user(self) -> AsyncIterator[object]:
        """Own order/trade updates. Requires the secure client.

        This is the fastest path to fill confirmation, and therefore the primary
        input to resolving an uncertain submission.

        Two shapes arrive on this topic. ``order`` events carry
        ``order_event_type`` (PLACEMENT / UPDATE / CANCELLATION) and a status of
        LIVE / MATCHED / DELAYED / UNMATCHED / CANCELED. ``trade`` events carry a
        *settlement* status -- MATCHED, MATCHED_NOT_BROADCASTED, MINED, CONFIRMED,
        RETRYING, FAILED.

        A trade is not final at MATCHED. Booking the position there and never
        following the transition to CONFIRMED (or FAILED) is how a phantom
        position enters the book through the path that was supposed to be the
        authoritative one. Only CONFIRMED settles; FAILED must raise
        ``TradeSettlementFailedError`` and trip SETTLEMENT_FAILURE.
        """
        raise NotImplementedError("PolymarketStreams.subscribe_user")

    # --- Health -----------------------------------------------------------
    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def reconnect_count(self) -> int:
        return self._reconnects

    @property
    def dropped_events(self) -> int:
        return self._dropped

    def book_for(self, token_id: ClobTokenId) -> BookState | None:
        """Current folded state, for REST cross-checks and re-anchoring."""
        return self._books.get(token_id)

    @property
    def last_event_at(self) -> datetime | None:
        """When the connection last delivered anything.

        The freshness signal for every folded book. A breaker watching for a silent
        feed reads this rather than any individual book's timestamp, because one
        quiet market says nothing about the socket.
        """
        return self._last_event_at
