"""Data-quality verdicts.

The asymmetry these tests protect: only FRESH permits a new entry, while DEGRADED
still permits exits. Getting the boundary wrong in one direction stops the system
trading; in the other it trades on data that has already expired.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.config.thresholds import Thresholds
from deepflow.core.clock import ManualClock
from deepflow.core.domain import BookLevel, MarketSnapshot, OrderBook
from deepflow.core.enums import DataQuality
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.pipeline.features import SEVERITY, FeatureEngine

NOW = datetime(2026, 9, 12, 6, 0, tzinfo=UTC)
TOKEN = ClobTokenId("tok")
COND = ConditionId("0xcond")

#: Default breaker budget is 10s, degrading past 50% of it.
BUDGET = 10.0


@pytest.fixture
def engine() -> FeatureEngine:
    return FeatureEngine(Thresholds(), ManualClock(NOW))


def _book(
    *,
    bids: list[tuple[str, str]] | None = None,
    asks: list[tuple[str, str]] | None = None,
    captured_at: datetime = NOW,
) -> OrderBook:
    return OrderBook(
        token_id=TOKEN,
        bids=tuple(
            BookLevel(price=Decimal(p), size=Decimal(s))
            for p, s in (bids if bids is not None else [("0.90", "100")])
        ),
        asks=tuple(
            BookLevel(price=Decimal(p), size=Decimal(s))
            for p, s in (asks if asks is not None else [("0.91", "100")])
        ),
        captured_at=captured_at,
    )


# --- Freshness ------------------------------------------------------------
def test_current_snapshot_is_fresh(engine: FeatureEngine) -> None:
    assert engine.assess_quality(captured_at=NOW, book=_book()) is DataQuality.FRESH


@pytest.mark.parametrize("age", [0.0, 1.0, 4.9])
def test_inside_the_grey_zone_is_fresh(engine: FeatureEngine, age: float) -> None:
    captured = NOW - timedelta(seconds=age)
    assert engine.assess_quality(captured_at=captured, book=_book()) is DataQuality.FRESH


@pytest.mark.parametrize("age", [5.1, 7.0, 9.9])
def test_approaching_the_budget_degrades(engine: FeatureEngine, age: float) -> None:
    """A snapshot at 90% of its budget expires before an order is acknowledged, so
    it must stop new entries while still allowing exits."""
    captured = NOW - timedelta(seconds=age)
    assert engine.assess_quality(captured_at=captured, book=_book()) is DataQuality.DEGRADED


@pytest.mark.parametrize("age", [10.1, 60.0, 600.0])
def test_past_the_budget_is_stale(engine: FeatureEngine, age: float) -> None:
    captured = NOW - timedelta(seconds=age)
    assert engine.assess_quality(captured_at=captured, book=_book()) is DataQuality.STALE


def test_caller_can_tighten_the_budget(engine: FeatureEngine) -> None:
    """Per-strategy budgets are tighter than the breaker's: 5s for an entry versus
    10s before the breaker trips."""
    captured = NOW - timedelta(seconds=6)
    assert engine.assess_quality(captured_at=captured, book=_book()) is DataQuality.DEGRADED
    assert (
        engine.assess_quality(captured_at=captured, book=_book(), max_age_seconds=5.0)
        is DataQuality.STALE
    )


# --- Stream gaps ----------------------------------------------------------
def test_a_gapped_book_is_never_fresh(engine: FeatureEngine) -> None:
    """The whole reason for tracking gaps: a book folded across a reconnect may be
    missing the update that moved it, so its timestamp being recent proves nothing."""
    assert (
        engine.assess_quality(captured_at=NOW, book=_book(), has_gap=True) is DataQuality.DEGRADED
    )


def test_a_gap_does_not_rescue_a_stale_book(engine: FeatureEngine) -> None:
    captured = NOW - timedelta(seconds=30)
    assert (
        engine.assess_quality(captured_at=captured, book=_book(), has_gap=True) is DataQuality.STALE
    )


# --- Consistency ----------------------------------------------------------
def test_empty_book_is_inconsistent(engine: FeatureEngine) -> None:
    book = _book(bids=[], asks=[])
    assert engine.assess_quality(captured_at=NOW, book=book) is DataQuality.INCONSISTENT


def test_one_sided_book_is_not_inconsistent(engine: FeatureEngine) -> None:
    """A book with no asks is legitimate -- it simply cannot support a buy. Calling
    it inconsistent would conflate 'no liquidity' with 'broken data'."""
    book = _book(asks=[])
    assert engine.assess_quality(captured_at=NOW, book=book) is DataQuality.FRESH


def test_non_positive_size_is_inconsistent(engine: FeatureEngine) -> None:
    """A zero-size level should have been removed. Its presence means a fold went
    wrong, and walking the book through it produces a fill that cannot happen."""
    book = _book(bids=[("0.90", "0")])
    assert engine.assess_quality(captured_at=NOW, book=book) is DataQuality.INCONSISTENT


@pytest.mark.parametrize("price", ["0", "1", "1.5"])
def test_price_outside_the_open_interval_is_inconsistent(engine: FeatureEngine, price: str) -> None:
    """Outcome tokens settle at 0 or 1, so a resting price at or beyond the bounds
    is a corrupt level rather than a wide quote."""
    book = _book(bids=[(price, "100")], asks=[])
    assert engine.assess_quality(captured_at=NOW, book=book) is DataQuality.INCONSISTENT


def test_consistency_is_checked_before_age(engine: FeatureEngine) -> None:
    """An inconsistent book that is also old reports INCONSISTENT, not STALE.
    Reporting staleness would send someone hunting a latency problem that is not
    there."""
    book = _book(bids=[], asks=[], captured_at=NOW - timedelta(hours=1))
    assert (
        engine.assess_quality(captured_at=NOW - timedelta(hours=1), book=book)
        is DataQuality.INCONSISTENT
    )


# --- Clock skew -----------------------------------------------------------
def test_small_clock_skew_is_tolerated(engine: FeatureEngine) -> None:
    """Timestamps are assigned server-side before the frame reaches us, so being a
    fraction of a second ahead is normal."""
    captured = NOW + timedelta(seconds=1)
    assert engine.assess_quality(captured_at=captured, book=_book()) is DataQuality.FRESH


def test_a_snapshot_from_the_future_is_inconsistent(engine: FeatureEngine) -> None:
    """The dangerous case: a negative age passes every freshness comparison
    trivially, so a skewed clock silently disables the check meant to protect us."""
    captured = NOW + timedelta(minutes=5)
    assert engine.assess_quality(captured_at=captured, book=_book()) is DataQuality.INCONSISTENT


# --- Whole snapshots ------------------------------------------------------
def _snapshot(*books: OrderBook, quality: DataQuality = DataQuality.FRESH) -> MarketSnapshot:
    return MarketSnapshot(
        condition_id=COND,
        books=books,
        captured_at=max((b.captured_at for b in books), default=NOW),
        quality=quality,
    )


def test_snapshot_takes_the_worst_book(engine: FeatureEngine) -> None:
    """Worst-of, not average: a market with one current book and one two-minute-old
    book is not half-fresh. Pricing either side needs both, since the complement is
    what shows the two agree."""
    fresh = _book()
    stale = _book(captured_at=NOW - timedelta(minutes=2))
    assert engine.assess_snapshot(_snapshot(fresh, stale)) is DataQuality.STALE


def test_snapshot_respects_quality_already_set_by_the_stream(
    engine: FeatureEngine,
) -> None:
    """The stream may have marked a snapshot DEGRADED for a reason timestamps cannot
    show -- a dropped event, a reconnect. Re-deriving quality from age alone would
    launder that away."""
    snapshot = _snapshot(_book(), quality=DataQuality.DEGRADED)
    assert engine.assess_snapshot(snapshot) is DataQuality.DEGRADED


def test_snapshot_with_no_books_is_inconsistent(engine: FeatureEngine) -> None:
    """Not a fresh market with no depth -- the absence of any observation at all."""
    assert engine.assess_snapshot(_snapshot()) is DataQuality.INCONSISTENT


def test_all_fresh_books_stay_fresh(engine: FeatureEngine) -> None:
    assert engine.assess_snapshot(_snapshot(_book(), _book())) is DataQuality.FRESH


def test_inconsistent_outranks_stale(engine: FeatureEngine) -> None:
    """Stale data was true at a past moment; inconsistent data was never true.
    The ranking matters because worst-of is how a snapshot verdict is formed."""
    assert SEVERITY[DataQuality.INCONSISTENT] > SEVERITY[DataQuality.STALE]
    assert SEVERITY[DataQuality.STALE] > SEVERITY[DataQuality.DEGRADED]
    assert SEVERITY[DataQuality.DEGRADED] > SEVERITY[DataQuality.FRESH]
