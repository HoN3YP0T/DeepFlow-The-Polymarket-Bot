"""Stream adapter: event fan-out, gap marking, and overflow behaviour.

Events are fed to the handler directly rather than through a fake socket. The
socket is the SDK's problem; what this adapter owns is what happens to an event
once it arrives, and that is where the mistakes live.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from deepflow.adapters.polymarket.streams import QUEUE_MAXSIZE, PolymarketStreams
from deepflow.config.settings import Settings
from deepflow.core.enums import DataQuality
from deepflow.core.types import ClobTokenId, ConditionId

T0 = datetime(2026, 9, 12, 5, 0, tzinfo=UTC)
COND = "0xcond"
YES = "tok_yes"
NO = "tok_no"


def _level(price: str, size: str) -> Any:
    return SimpleNamespace(price=Decimal(price), size=Decimal(size))


def _book_event(token_id: str, *, bids: list[Any], asks: list[Any]) -> Any:
    """A ``book`` event in the venue's wire order (worst price first)."""
    return SimpleNamespace(
        topic="market",
        type="book",
        payload=SimpleNamespace(
            asset_id=token_id,
            token_id=token_id,
            condition_id=COND,
            market=COND,
            bids=bids,
            asks=asks,
            timestamp=T0,
            tick_size=Decimal("0.001"),
            # The stream leaves these None even though REST populates them.
            min_order_size=None,
            neg_risk=None,
        ),
    )


def _price_change(*changes: Any) -> Any:
    return SimpleNamespace(
        topic="market",
        type="price_change",
        payload=SimpleNamespace(
            condition_id=COND, market=COND, timestamp=T0, price_changes=list(changes)
        ),
    )


def _change(
    token_id: str,
    *,
    side: str,
    price: str,
    size: str,
    best_bid: str | None = None,
    best_ask: str | None = None,
) -> Any:
    return SimpleNamespace(
        asset_id=token_id,
        token_id=token_id,
        side=side,
        price=Decimal(price),
        size=Decimal(size),
        best_bid=Decimal(best_bid) if best_bid else None,
        best_ask=Decimal(best_ask) if best_ask else None,
        hash="abc",
    )


@pytest.fixture
def streams() -> PolymarketStreams:
    return PolymarketStreams(SimpleNamespace(public=None), Settings())  # type: ignore[arg-type]


def _seed(streams: PolymarketStreams) -> None:
    """Both sides of a binary market, priced as complements."""
    streams._handle(
        _book_event(
            YES,
            bids=[_level("0.039", "50"), _level("0.040", "100")],
            asks=[_level("0.042", "80"), _level("0.041", "150")],
        )
    )
    streams._handle(
        _book_event(
            NO,
            bids=[_level("0.958", "50"), _level("0.959", "100")],
            asks=[_level("0.961", "80"), _level("0.960", "150")],
        )
    )
    while not streams._market_queue.empty():
        streams._market_queue.get_nowait()


# --- Fan-out --------------------------------------------------------------
def test_book_event_is_folded_and_normalized(streams: PolymarketStreams) -> None:
    """Stream books arrive in the same reversed wire order as REST, so the same
    best-first normalization applies."""
    _seed(streams)
    state = streams.book_for(ClobTokenId(YES))
    assert state is not None
    assert state.best_bid == Decimal("0.040")
    assert state.best_ask == Decimal("0.041")


def test_snapshot_carries_every_book_for_the_market(streams: PolymarketStreams) -> None:
    """One snapshot per market, both outcomes included, so a consumer can check
    the complement without a second call."""
    _seed(streams)
    snapshot = streams._snapshot_for(ConditionId(COND), {ClobTokenId(YES), ClobTokenId(NO)})
    assert snapshot is not None
    assert len(snapshot.books) == 2
    total = sum(b.best_ask for b in snapshot.books if b.best_ask)
    assert Decimal("0.99") < total < Decimal("1.02")


def test_one_frame_can_update_both_sides(streams: PolymarketStreams) -> None:
    """Observed live: a single price_change frame carries changes for YES and NO."""
    _seed(streams)
    streams._handle(
        _price_change(
            _change(
                YES, side="SELL", price="0.041", size="999", best_bid="0.040", best_ask="0.041"
            ),
            _change(NO, side="BUY", price="0.959", size="888", best_bid="0.959", best_ask="0.960"),
        )
    )
    yes = streams.book_for(ClobTokenId(YES))
    no = streams.book_for(ClobTokenId(NO))
    assert yes is not None and yes.asks[Decimal("0.041")] == Decimal("999")
    assert no is not None and no.bids[Decimal("0.959")] == Decimal("888")


def test_sports_events_go_to_their_own_queue(streams: PolymarketStreams) -> None:
    streams._handle(
        SimpleNamespace(
            topic="sports",
            type="sport_result",
            payload=SimpleNamespace(
                game_id=5127839,
                league_abbreviation="NBA",
                status="InProgress",
                live=True,
                ended=False,
                score="98-94",
                period="Q4",
                elapsed="05:12",
            ),
        )
    )
    assert streams._market_queue.empty()
    event = streams._sports_queue.get_nowait()
    assert event.scores() == (98, 94)


def test_change_for_an_unknown_token_is_ignored(streams: PolymarketStreams) -> None:
    """Applying a level to a book we have never snapshotted would build a book out
    of fragments and present it as real."""
    streams._handle(_price_change(_change("never_seen", side="BUY", price="0.5", size="10")))
    assert streams.book_for(ClobTokenId("never_seen")) is None


def test_unknown_event_types_are_ignored(streams: PolymarketStreams) -> None:
    streams._handle(SimpleNamespace(topic="market", type="something_new", payload=None))
    streams._handle(SimpleNamespace(topic="perps", type="trade", payload=None))


# --- Tick size ------------------------------------------------------------
def test_tick_size_change_overwrites_the_cached_value(streams: PolymarketStreams) -> None:
    """Orders priced on a stale grid are rejected outright, so a cached tick must
    be replaced the moment the venue says so."""
    _seed(streams)
    streams._handle(
        SimpleNamespace(
            topic="market",
            type="tick_size_change",
            payload=SimpleNamespace(
                asset_id=YES, condition_id=COND, timestamp=T0, new_tick_size=Decimal("0.01")
            ),
        )
    )
    state = streams.book_for(ClobTokenId(YES))
    assert state is not None and state.tick_size == Decimal("0.01")


# --- Gaps and quality ----------------------------------------------------
def test_reconnect_marks_every_book_gapped(streams: PolymarketStreams) -> None:
    _seed(streams)
    streams._mark_all_gapped()
    assert all(state.has_gap for state in streams._books.values())


def test_gapped_book_degrades_snapshot_quality(streams: PolymarketStreams) -> None:
    """DEGRADED is usable for monitoring and exits but blocks new entries:
    reducing an uncertain position shrinks the problem, opening one on stale depth
    compounds it."""
    _seed(streams)
    fresh = streams._snapshot_for(ConditionId(COND), {ClobTokenId(YES), ClobTokenId(NO)})
    assert fresh is not None and fresh.quality is DataQuality.FRESH

    streams._mark_all_gapped()
    degraded = streams._snapshot_for(ConditionId(COND), {ClobTokenId(YES), ClobTokenId(NO)})
    assert degraded is not None and degraded.quality is DataQuality.DEGRADED


def test_drift_against_the_reported_touch_marks_a_gap(streams: PolymarketStreams) -> None:
    """The venue sends the touch with every change, so a dropped update is caught
    immediately rather than at the next REST poll."""
    _seed(streams)
    streams._handle(
        _price_change(
            _change(YES, side="BUY", price="0.039", size="10", best_bid="0.20", best_ask="0.21")
        )
    )
    state = streams.book_for(ClobTokenId(YES))
    assert state is not None and state.has_gap


def test_snapshot_only_includes_watched_tokens(streams: PolymarketStreams) -> None:
    _seed(streams)
    snapshot = streams._snapshot_for(ConditionId(COND), {ClobTokenId(YES)})
    assert snapshot is not None
    assert [b.token_id for b in snapshot.books] == [YES]


def test_unknown_market_yields_no_snapshot(streams: PolymarketStreams) -> None:
    assert streams._snapshot_for(ConditionId("0xnope"), {ClobTokenId(YES)}) is None


# --- Backpressure --------------------------------------------------------
def test_overflow_drops_oldest_and_flags_a_gap(streams: PolymarketStreams) -> None:
    """A blocked put would stall the only reader of the socket, so the queue drops
    instead. For book data the newest frame is the valuable one, which makes the
    oldest the right thing to lose -- but a dropped update is a hole, so the books
    are flagged rather than passing silently."""
    _seed(streams)
    for _ in range(QUEUE_MAXSIZE + 5):
        streams._offer(streams._market_queue, ConditionId(COND))

    assert streams._market_queue.qsize() <= QUEUE_MAXSIZE
    assert streams.dropped_events > 0
    assert all(state.has_gap for state in streams._books.values())


def test_health_counters_start_clean(streams: PolymarketStreams) -> None:
    assert not streams.is_connected
    assert streams.reconnect_count == 0
    assert streams.dropped_events == 0


# --- Freshness signal -----------------------------------------------------
def test_any_event_advances_feed_liveness(streams: PolymarketStreams) -> None:
    """Liveness belongs to the socket. An event on a topic we ignore still proves
    the connection is delivering."""
    assert streams.last_event_at is None
    streams._handle(SimpleNamespace(topic="perps", type="trade", payload=None))
    assert streams.last_event_at is not None


def test_snapshots_are_stamped_with_liveness_not_last_change(
    streams: PolymarketStreams,
) -> None:
    """The bug this guards, found on a live run: 4 of 120 snapshots came back STALE
    on a healthy connection with zero reconnects, because a quiet market's book was
    timestamped with its own last change rather than the feed's."""
    _seed(streams)
    for state in streams._books.values():
        assert state.updated_at == T0

    live = streams.last_event_at
    assert live is not None and live > T0

    snapshot = streams._snapshot_for(ConditionId(COND), {ClobTokenId(YES), ClobTokenId(NO)})
    assert snapshot is not None
    assert all(book.captured_at == live for book in snapshot.books)
