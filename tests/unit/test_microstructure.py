"""Order-flow features and the flow assessment built on them.

The recurring theme: every feature returns ``None`` rather than a plausible number
when its inputs will not support one. A fabricated zero reads downstream as a real,
balanced measurement, and the whole reason these fields are optional is that "no
information" is a different claim from "neutral".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.config.thresholds import Thresholds
from deepflow.core.clock import SystemClock
from deepflow.core.domain import BookLevel, MarketSnapshot, Microstructure, OrderBook, PublicTrade
from deepflow.core.enums import OrderSide
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.engines.microstructure import FlowDirection, MicrostructureEngine
from deepflow.pipeline.features import FeatureEngine

NOW = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
TOKEN = ClobTokenId("tok")
COND = ConditionId("0xabc")


@pytest.fixture
def features() -> FeatureEngine:
    return FeatureEngine(Thresholds(), SystemClock())


@pytest.fixture
def engine() -> MicrostructureEngine:
    return MicrostructureEngine(Thresholds())


def _book(
    bids: list[tuple[str, str]],
    asks: list[tuple[str, str]],
    *,
    at: datetime = NOW,
) -> OrderBook:
    return OrderBook(
        token_id=TOKEN,
        bids=tuple(BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in bids),
        asks=tuple(BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks),
        captured_at=at,
    )


def _trade(side: OrderSide, size: str) -> PublicTrade:
    return PublicTrade(
        token_id=TOKEN, price=Decimal("0.50"), size=Decimal(size), side=side, traded_at=NOW
    )


# --- Book imbalance: the band is the definition ---------------------------
def test_imbalance_counts_only_depth_near_the_touch(features: FeatureEngine) -> None:
    """Depth parked far from the touch is not tradeable interest. Measured on live
    books, whole-book imbalance is dominated by dust resting at 0.001 and 0.999."""
    book = _book(
        # 1000 inside the band, a million of dust far below it
        [("0.50", "1000"), ("0.10", "1000000")],
        [("0.51", "1000")],
    )
    micro = features.compute(book=book)
    assert micro.book_imbalance == 0  # balanced inside the band


def test_imbalance_is_signed_toward_the_heavier_side(features: FeatureEngine) -> None:
    book = _book([("0.50", "3000")], [("0.51", "1000")])
    imbalance = features.compute(book=book).book_imbalance
    assert imbalance is not None and imbalance > Decimal("0.4")


def test_imbalance_is_none_when_the_sign_is_not_robust(features: FeatureEngine) -> None:
    """Observed live on 3 of 5 real books: the sign flips between a one-cent and a
    five-cent band. An imbalance that depends on where the band was drawn is an
    artifact, not a property of the market."""
    book = _book(
        # bid-heavy within 1c, ask-heavy within 5c
        [("0.50", "5000"), ("0.46", "1000")],
        [("0.51", "500"), ("0.55", "50000")],
    )
    assert features.compute(book=book).book_imbalance is None


def test_imbalance_is_none_on_a_one_sided_book(features: FeatureEngine) -> None:
    assert features.compute(book=_book([("0.50", "1000")], [])).book_imbalance is None


def test_imbalance_is_none_below_the_minimum_depth(features: FeatureEngine) -> None:
    """A ratio of two tiny numbers is noise wearing a signal's clothing."""
    assert features.compute(book=_book([("0.50", "3")], [("0.51", "2")])).book_imbalance is None


# --- Flow imbalance -------------------------------------------------------
def test_flow_imbalance_is_signed_traded_volume(features: FeatureEngine) -> None:
    trades = (_trade(OrderSide.BUY, "75"), _trade(OrderSide.SELL, "25"))
    micro = features.compute(book=_book([("0.50", "1000")], [("0.51", "1000")]), trades=trades)
    assert micro.flow_imbalance == Decimal("0.5")


def test_no_trades_means_no_flow_measurement(features: FeatureEngine) -> None:
    assert (
        features.compute(book=_book([("0.50", "1000")], [("0.51", "1000")])).flow_imbalance is None
    )


# --- Liquidity concentration ---------------------------------------------
def test_concentration_reports_the_worse_side(features: FeatureEngine) -> None:
    """A trade needs the side it crosses; averaging the two would hide a hollow one."""
    book = _book(
        [("0.50", "500"), ("0.495", "500")],  # evenly spread
        [("0.51", "9500"), ("0.515", "500")],  # 95% at one level
    )
    concentration = features.compute(book=book).liquidity_concentration
    assert concentration is not None and concentration > Decimal("0.9")


def test_concentration_is_none_without_a_book(features: FeatureEngine) -> None:
    assert features.compute(book=_book([], [])).liquidity_concentration is None


# --- Slippage -------------------------------------------------------------
def test_size_that_fills_at_the_touch_has_no_slippage(features: FeatureEngine) -> None:
    book = _book([("0.50", "1000")], [("0.51", "1000")])
    assert features.compute(book=book, size_shares=Decimal(500)).estimated_slippage_bps == 0


def test_slippage_grows_as_the_walk_deepens(features: FeatureEngine) -> None:
    book = _book([("0.50", "1000")], [("0.51", "100"), ("0.60", "10000")])
    small = features.compute(book=book, size_shares=Decimal(100)).estimated_slippage_bps
    large = features.compute(book=book, size_shares=Decimal(1000)).estimated_slippage_bps
    assert small == 0
    assert large is not None and large > Decimal(1000)


def test_book_too_thin_returns_none_not_a_partial_estimate(
    features: FeatureEngine,
) -> None:
    """A partial walk reported as slippage understates the cost precisely when the
    book is too thin to trade."""
    book = _book([("0.50", "1000")], [("0.51", "10")])
    assert features.compute(book=book, size_shares=Decimal(10_000)).estimated_slippage_bps is None


def test_no_size_means_no_slippage_estimate(features: FeatureEngine) -> None:
    book = _book([("0.50", "1000")], [("0.51", "1000")])
    assert features.compute(book=book).estimated_slippage_bps is None


# --- Velocity, acceleration, abnormality ---------------------------------
def _history(mids: list[str]) -> tuple[OrderBook, ...]:
    return tuple(
        _book(
            [(str(Decimal(m) - Decimal("0.005")), "1000")],
            [(str(Decimal(m) + Decimal("0.005")), "1000")],
            at=NOW + timedelta(seconds=i),
        )
        for i, m in enumerate(mids)
    )


def test_velocity_needs_enough_history(features: FeatureEngine) -> None:
    """Two points give a slope with no way to tell a trend from a single tick."""
    assert (
        features.compute(
            book=_book([("0.5", "1000")], [("0.51", "1000")]), history=_history(["0.50", "0.51"])
        ).price_velocity
        is None
    )


def test_velocity_is_mid_change_per_second(features: FeatureEngine) -> None:
    history = _history(["0.50", "0.51", "0.52"])
    velocity = features.compute(book=history[-1], history=history).price_velocity
    assert velocity is not None and velocity == Decimal("0.01")


def test_acceleration_needs_twice_the_history(features: FeatureEngine) -> None:
    """A single slope cannot accelerate."""
    assert (
        features.compute(
            book=_book([("0.5", "1000")], [("0.51", "1000")]),
            history=_history(["0.50", "0.51", "0.52"]),
        ).price_acceleration
        is None
    )


def test_abnormal_move_is_measured_against_the_markets_own_volatility(
    features: FeatureEngine,
) -> None:
    """A 2c move is nothing on a 0.50 market and enormous on a 0.02 one, so a fixed
    threshold would either ignore the second or fire constantly on the first."""
    quiet = _history(["0.500", "0.501", "0.502", "0.503"])
    assert not features.compute(book=quiet[-1], history=quiet).abnormal_move

    jumpy = _history(["0.500", "0.501", "0.502", "0.600"])
    assert features.compute(book=jumpy[-1], history=jumpy).abnormal_move


def test_a_flat_book_does_not_become_abnormal_on_its_first_tick(
    features: FeatureEngine,
) -> None:
    """Otherwise every market that has simply been quiet flags the moment it moves."""
    flat = _history(["0.50", "0.50", "0.50", "0.51"])
    assert not features.compute(book=flat[-1], history=flat).abnormal_move


# --- Flow assessment -----------------------------------------------------
def _snapshot(micro: Microstructure, *, book: OrderBook | None = None) -> MarketSnapshot:
    resolved = book or _book([("0.50", "1000")], [("0.51", "1000")])
    return MarketSnapshot(
        condition_id=COND, books=(resolved,), microstructure=micro, captured_at=NOW
    )


def test_missing_inputs_are_unknown_not_neutral(engine: MicrostructureEngine) -> None:
    """Collapsing them lets a market with no measurable flow pass a confirmation gate
    as though flow had been checked and found unobjectionable."""
    result = engine.assess(
        snapshot=_snapshot(Microstructure()), token_id=TOKEN, size_shares=Decimal(100)
    )
    assert result.direction is FlowDirection.UNKNOWN
    assert result.score == 0


def test_balanced_flow_is_neutral(engine: MicrostructureEngine) -> None:
    micro = Microstructure(
        book_imbalance=Decimal(0),
        flow_imbalance=Decimal(0),
        liquidity_concentration=Decimal("0.2"),
    )
    result = engine.assess(snapshot=_snapshot(micro), token_id=TOKEN, size_shares=Decimal(100))
    assert result.direction is FlowDirection.NEUTRAL
    assert result.liquidity_ok


def test_traded_flow_outweighs_resting_book_depth(engine: MicrostructureEngine) -> None:
    """Resting intent can be withdrawn; traded flow already happened. Equal weighting
    would let a wall of cancellable orders outvote actual transactions."""
    micro = Microstructure(
        book_imbalance=Decimal("1"),
        flow_imbalance=Decimal("-1"),
        liquidity_concentration=Decimal("0.2"),
    )
    result = engine.assess(snapshot=_snapshot(micro), token_id=TOKEN, size_shares=Decimal(100))
    assert result.score < 0


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        ("0.9", FlowDirection.STRONG_BUY),
        ("0.2", FlowDirection.BUY),
        ("0.0", FlowDirection.NEUTRAL),
        ("-0.2", FlowDirection.SELL),
        ("-0.9", FlowDirection.STRONG_SELL),
    ],
)
def test_direction_bands(engine: MicrostructureEngine, score: str, expected: FlowDirection) -> None:
    micro = Microstructure(flow_imbalance=Decimal(score), liquidity_concentration=Decimal("0.2"))
    result = engine.assess(snapshot=_snapshot(micro), token_id=TOKEN, size_shares=Decimal(100))
    assert result.direction is expected


def test_concentrated_depth_fails_the_liquidity_check(
    engine: MicrostructureEngine,
) -> None:
    """Observed on 63-91% of real politics books: banded depth sitting almost
    entirely at one level, which is one cancellation from empty however large the
    total looks."""
    micro = Microstructure(flow_imbalance=Decimal("0.1"), liquidity_concentration=Decimal("0.91"))
    result = engine.assess(snapshot=_snapshot(micro), token_id=TOKEN, size_shares=Decimal(100))
    assert not result.liquidity_ok
    assert any("concentrated" in note for note in result.notes)


def test_unmeasurable_concentration_reads_as_not_ok(
    engine: MicrostructureEngine,
) -> None:
    """Assuming a book is fine because we could not measure it is the failure this
    module exists to prevent."""
    micro = Microstructure(flow_imbalance=Decimal("0.1"))
    result = engine.assess(snapshot=_snapshot(micro), token_id=TOKEN, size_shares=Decimal(100))
    assert not result.liquidity_ok


def test_missing_book_is_unknown(engine: MicrostructureEngine) -> None:
    snapshot = MarketSnapshot(condition_id=COND, books=(), captured_at=NOW)
    result = engine.assess(
        snapshot=snapshot, token_id=ClobTokenId("other"), size_shares=Decimal(100)
    )
    assert result.direction is FlowDirection.UNKNOWN
    assert not result.liquidity_ok
