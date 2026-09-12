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
from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from deepflow.adapters.polymarket.book_state import BookState
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.adapters.polymarket.sports_feed import SportsFeedEvent
from deepflow.config.settings import Settings
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import MarketSnapshot, Microstructure
from deepflow.core.enums import DataQuality
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId, ConditionId

log = get_logger(__name__)

#: Per-feed queue depth. Deep enough to ride out a GC pause or a slow database
#: write, shallow enough that a wedged consumer is noticed in seconds rather than
#: consuming memory until the process dies.
QUEUE_MAXSIZE = 1000

#: Events whose ``type`` we fold into book state.
_MARKET_TYPES = frozenset(
    {"book", "price_change", "tick_size_change", "best_bid_ask", "last_trade_price"}
)


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
    async def subscribe_markets(
        self, token_ids: Sequence[ClobTokenId]
    ) -> AsyncIterator[MarketSnapshot]:
        """Stream normalized snapshots for the given tokens.

        Yields one :class:`MarketSnapshot` per updated market, carrying every book
        we hold for that market's tokens -- both sides of a binary, so a consumer
        can check the complement without a second call.

        ``quality`` is ``DEGRADED`` while any contributing book is flagged gapped.
        Degraded state is explicitly usable for monitoring and exits but blocks
        new entries: reducing an uncertain position shrinks the problem, opening
        one on stale depth compounds it.
        """
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
            captured_at=max(book.captured_at for book in books),
            quality=DataQuality.DEGRADED if gapped else DataQuality.FRESH,
        )

    async def subscribe_sports(self) -> AsyncIterator[SportsFeedEvent]:
        """Live game state.

        The venue streams every game with no server-side filter, so callers must
        match on ``game_id`` against ``Market.game_id``. See :mod:`sports_feed`
        for how little the payload actually carries.
        """
        while not self._stop.is_set():
            event = await self._sports_queue.get()
            try:
                yield event
            finally:
                self._sports_queue.task_done()

    def subscribe_crypto_prices(
        self, symbols: Sequence[str], *, source: str = "binance"
    ) -> AsyncIterator[object]:
        """Reference prices for crypto markets.

        ``source`` selects the topic and therefore the symbol format, and the
        choice is not cosmetic: a market settling against a Chainlink TWAP priced
        off Binance spot is mispriced by the basis between them, and at the short
        horizons this system trades that basis is the whole edge. Which feed a
        market settles against is read from its resolution criteria; a mismatch is
        an abstention, not an approximation.
        """
        raise NotImplementedError("PolymarketStreams.subscribe_crypto_prices")

    def subscribe_crypto_twap(
        self, symbols: Sequence[str], *, window_seconds: int = 30
    ) -> AsyncIterator[object]:
        """Chainlink-computed TWAP prices. Windows of 30 or 60 seconds only."""
        raise NotImplementedError("PolymarketStreams.subscribe_crypto_twap")

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
