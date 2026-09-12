"""Incremental book folding.

Semantics here were verified against the live feed before being written down:
420 level changes folded across 30 books, then compared against fresh REST
snapshots -- 8 of 8 agreed at the touch. These tests pin that behaviour so a
later "simplification" into delta arithmetic fails loudly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.adapters.polymarket.book_state import BookState
from deepflow.core.types import ClobTokenId

T0 = datetime(2026, 9, 12, 5, 0, tzinfo=UTC)
TOKEN = ClobTokenId("tok")


def _state() -> BookState:
    state = BookState(token_id=TOKEN)
    state.apply_snapshot(
        bids=[(Decimal("0.90"), Decimal(100)), (Decimal("0.89"), Decimal(200))],
        asks=[(Decimal("0.91"), Decimal(150)), (Decimal("0.92"), Decimal(250))],
        timestamp=T0,
        tick_size=Decimal("0.01"),
    )
    return state


# --- Snapshot -------------------------------------------------------------
def test_snapshot_is_sorted_best_first() -> None:
    book = _state().snapshot()
    assert book is not None
    assert [level.price for level in book.bids] == [Decimal("0.90"), Decimal("0.89")]
    assert [level.price for level in book.asks] == [Decimal("0.91"), Decimal("0.92")]
    assert book.best_bid == Decimal("0.90")
    assert book.best_ask == Decimal("0.91")


def test_snapshot_replaces_rather_than_merges() -> None:
    """A ``book`` event is ground truth. Merging it into existing state would keep
    levels the venue has since removed."""
    state = _state()
    state.apply_snapshot(
        bids=[(Decimal("0.50"), Decimal(10))],
        asks=[(Decimal("0.51"), Decimal(10))],
        timestamp=T0,
    )
    assert state.best_bid == Decimal("0.50")
    assert len(state.bids) == 1


def test_zero_sized_levels_in_a_snapshot_are_dropped() -> None:
    state = BookState(token_id=TOKEN)
    state.apply_snapshot(
        bids=[(Decimal("0.90"), Decimal(100)), (Decimal("0.89"), Decimal(0))],
        asks=[],
        timestamp=T0,
    )
    assert list(state.bids) == [Decimal("0.90")]


def test_no_snapshot_means_no_book() -> None:
    """An empty book must not read downstream as a real, illiquid market."""
    assert BookState(token_id=TOKEN).snapshot() is None


# --- Level changes are replacements, not deltas ---------------------------
def test_level_change_replaces_the_size() -> None:
    """``size`` is the level's new total. Adding instead would inflate depth
    without bound, and every slippage and liquidity figure with it -- in the
    direction that makes trades look safer."""
    state = _state()
    state.apply_level(side="BUY", price=Decimal("0.90"), size=Decimal(500), timestamp=T0)
    assert state.bids[Decimal("0.90")] == Decimal(500)


def test_repeated_identical_changes_are_idempotent() -> None:
    """The distinguishing test between replacement and delta semantics: applying
    the same frame twice must not double the level."""
    state = _state()
    for _ in range(3):
        state.apply_level(side="BUY", price=Decimal("0.90"), size=Decimal(400), timestamp=T0)
    assert state.bids[Decimal("0.90")] == Decimal(400)


def test_zero_size_removes_the_level() -> None:
    state = _state()
    state.apply_level(side="SELL", price=Decimal("0.91"), size=Decimal(0), timestamp=T0)
    assert Decimal("0.91") not in state.asks
    assert state.best_ask == Decimal("0.92")


def test_a_new_price_adds_a_level() -> None:
    state = _state()
    state.apply_level(side="BUY", price=Decimal("0.905"), size=Decimal(50), timestamp=T0)
    assert state.best_bid == Decimal("0.905")


def test_a_deep_level_change_does_not_move_the_touch() -> None:
    """Observed live: a 20,000-share bid at 0.10 on a market trading at 0.92
    arrives as a price_change at 0.10. Treating ``price`` as the new best would
    look like an 82-cent crash."""
    state = _state()
    state.apply_level(
        side="BUY",
        price=Decimal("0.10"),
        size=Decimal(20_000),
        timestamp=T0,
        best_bid=Decimal("0.90"),
        best_ask=Decimal("0.91"),
    )
    assert state.best_bid == Decimal("0.90")
    assert not state.drifted()


# --- Drift detection ------------------------------------------------------
def test_drift_detected_when_our_touch_disagrees_with_the_venue() -> None:
    """The venue reports the touch with every change, so a dropped or misapplied
    update is detectable immediately rather than at the next REST poll."""
    state = _state()
    state.apply_level(
        side="BUY",
        price=Decimal("0.89"),
        size=Decimal(300),
        timestamp=T0,
        best_bid=Decimal("0.95"),  # venue says 0.95; we hold 0.90
        best_ask=Decimal("0.96"),
    )
    assert state.drifted()


def test_no_reported_touch_is_not_treated_as_agreement() -> None:
    state = _state()
    state.apply_level(side="BUY", price=Decimal("0.90"), size=Decimal(10), timestamp=T0)
    assert not state.drifted()


def test_matching_touch_is_not_drift() -> None:
    state = _state()
    state.apply_level(
        side="BUY",
        price=Decimal("0.90"),
        size=Decimal(10),
        timestamp=T0,
        best_bid=Decimal("0.90"),
        best_ask=Decimal("0.91"),
    )
    assert not state.drifted()


# --- Gaps -----------------------------------------------------------------
def test_gap_survives_incremental_updates() -> None:
    """An incremental update on top of a book with a hole in it is still a book
    with a hole in it. Only a fresh snapshot clears the flag."""
    state = _state()
    state.mark_gap()
    state.apply_level(side="BUY", price=Decimal("0.90"), size=Decimal(1), timestamp=T0)
    assert state.has_gap


def test_snapshot_clears_the_gap() -> None:
    state = _state()
    state.mark_gap()
    state.apply_snapshot(
        bids=[(Decimal("0.90"), Decimal(1))], asks=[], timestamp=T0 + timedelta(seconds=1)
    )
    assert not state.has_gap


# --- Crossed books --------------------------------------------------------
def test_crossed_book_is_dropped_and_flagged() -> None:
    """Crossing means our state is wrong -- typically a stale level that should
    have been removed. Catching it here turns an exception in the middle of the
    event pump into a recoverable, logged condition."""
    state = _state()
    state.apply_level(side="BUY", price=Decimal("0.95"), size=Decimal(10), timestamp=T0)
    assert state.snapshot() is None
    assert state.has_gap


def test_one_sided_book_is_still_a_book() -> None:
    """A book with no asks is legitimate and must not be discarded -- it just
    cannot support a buy."""
    state = BookState(token_id=TOKEN)
    state.apply_snapshot(bids=[(Decimal("0.90"), Decimal(100))], asks=[], timestamp=T0)
    book = state.snapshot()
    assert book is not None
    assert book.best_ask is None


@pytest.mark.parametrize("side", ["BUY", "buy", "Buy"])
def test_side_matching_is_case_insensitive(side: str) -> None:
    """The wire sends ``BUY``/``SELL``, but a mis-cased side silently landing on
    the wrong half of the book would corrupt it without error."""
    state = _state()
    state.apply_level(side=side, price=Decimal("0.90"), size=Decimal(7), timestamp=T0)
    assert state.bids[Decimal("0.90")] == Decimal(7)


# --- Freshness is feed liveness, not last change --------------------------
def test_snapshot_is_timestamped_with_feed_liveness() -> None:
    """``as_of`` overrides the book's own last-change time.

    A quiet book on a live feed is correct, not stale: on an order book, no update
    means no change. Measured live, last-change ages across sixteen tokens on one
    healthy connection spanned 1.4s to 30.7s -- timestamping by last change would
    have read a quarter of them as expired.
    """
    state = _state()
    later = T0 + timedelta(seconds=30)
    book = state.snapshot(as_of=later)
    assert book is not None
    assert book.captured_at == later
    assert state.updated_at == T0  # last change is kept, for diagnostics


def test_snapshot_falls_back_to_last_change_without_a_liveness_signal() -> None:
    """A replay or a test has no feed to ask, so it gets the conservative reading
    rather than a silently optimistic one."""
    state = _state()
    book = state.snapshot()
    assert book is not None
    assert book.captured_at == T0
