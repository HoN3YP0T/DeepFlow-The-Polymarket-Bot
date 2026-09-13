"""The EV calculation: units, the spread double-count, and refusals.

`net_ev` is the number that decides whether this system trades, so the tests here
are about the ways it can be wrong while looking right: mixing probability units
with basis points, charging the spread twice, or pricing an entry the book cannot
actually fill.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from deepflow.config.thresholds import Thresholds
from deepflow.core.domain import (
    BookLevel,
    FeeSchedule,
    Market,
    MarketSnapshot,
    OrderBook,
    Outcome,
    ProbabilityEstimate,
)
from deepflow.core.enums import DataQuality, OrderSide, OutcomeSide
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.engines.ev import EvEngine

YES = ClobTokenId("1")
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _market(*, rate: str = "0.04") -> Market:
    return Market(
        condition_id=ConditionId("0xabc"),
        question="Will it?",
        outcomes=(Outcome(token_id=YES, label="Yes", side=OutcomeSide.YES),),
        active=True,
        closed=False,
        accepting_orders=True,
        fees_enabled=True,
        fee_schedule=FeeSchedule(rate=Decimal(rate)),
    )


def _book(
    *,
    bid: str = "0.94",
    asks: tuple[tuple[str, str], ...] = (("0.95", "1000"),),
) -> OrderBook:
    return OrderBook(
        token_id=YES,
        bids=(BookLevel(price=Decimal(bid), size=Decimal(1000)),),
        asks=tuple(BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks),
        captured_at=NOW,
    )


def _snapshot(book: OrderBook, quality: DataQuality = DataQuality.FRESH) -> MarketSnapshot:
    return MarketSnapshot(
        condition_id=ConditionId("0xabc"),
        books=(book,),
        captured_at=NOW,
        quality=quality,
    )


def _estimate(*, probability: str = "0.98", uncertainty: str = "0.0") -> ProbabilityEstimate:
    return ProbabilityEstimate(
        token_id=YES,
        model_probability=Decimal(probability),
        calibrated_probability=Decimal(probability),
        uncertainty=Decimal(uncertainty),
        engine="test",
    )


def _assess(engine: EvEngine | None = None, **kwargs: object) -> object:
    engine = engine or EvEngine(Thresholds())
    book = kwargs.pop("book", None) or _book()
    return engine.assess(
        estimate=kwargs.pop("estimate", None) or _estimate(),  # type: ignore[arg-type]
        snapshot=kwargs.pop("snapshot", None) or _snapshot(book),  # type: ignore[arg-type]
        token_id=YES,
        size_shares=Decimal(str(kwargs.pop("size_shares", 100))),
        market=kwargs.pop("market", _market()),  # type: ignore[arg-type]
        **kwargs,  # type: ignore[arg-type]
    )


# --- The market price -----------------------------------------------------
def test_market_probability_is_the_ask_not_the_mid() -> None:
    """A mid-based edge books half the spread as profit on every single trade."""
    result = _assess()
    assert result is not None
    assert result.market_probability == Decimal("0.95")  # ask, not the 0.945 mid


def test_spread_is_not_charged_twice() -> None:
    """Crossing the spread is already inside the edge, so the cost term is zero.

    Both halves are asserted together because either alone reads as a bug: a zero
    spread cost looks like an omission, and a non-zero one next to an ask-based
    edge is a double charge that silently rejects profitable trades.
    """
    result = _assess()
    assert result is not None
    assert result.costs.spread_cost_bps == 0
    assert result.edge == Decimal("0.98") - Decimal("0.95")


# --- Units ----------------------------------------------------------------
def test_costs_convert_from_bps_of_notional_to_payoff_units() -> None:
    """The one place two unit systems meet, checked by hand.

    Costs are bps of notional; notional per share is the price. So a cost of
    ``b`` bps subtracts ``b / 10_000 * price`` from an edge measured in payoff.
    """
    result = _assess()
    assert result is not None
    expected = result.edge * result.fill_probability - (
        result.costs.total_bps / Decimal(10_000) * result.market_probability
    )
    assert result.net_ev == expected


def test_fee_is_never_silently_zero_when_a_market_is_given() -> None:
    result = _assess()
    assert result is not None
    assert result.costs.fee_bps > 0


def test_fee_is_omitted_rather_than_guessed_without_a_market() -> None:
    """Zero because the schedule is unknown -- and the caller has to have chosen it."""
    result = _assess(market=None)
    assert result is not None
    assert result.costs.fee_bps == 0


def test_a_thin_book_costs_more_than_a_deep_one() -> None:
    """Slippage is the walk past the touch, so depth shows up as a cost."""
    deep = _assess(book=_book(asks=(("0.95", "1000"),)), size_shares=100)
    thin = _assess(
        book=_book(asks=(("0.95", "10"), ("0.96", "40"), ("0.97", "1000"))),
        size_shares=100,
    )
    assert deep is not None and thin is not None
    assert deep.costs.slippage_bps == 0
    assert thin.costs.slippage_bps > 0
    assert thin.net_ev < deep.net_ev


# --- Refusals -------------------------------------------------------------
def test_book_too_thin_to_fill_is_no_assessment_at_all() -> None:
    """Not a bad price -- no price.

    A partial walk reported as a fill estimate understates the cost of entry at
    exactly the moment the book is too thin to enter.
    """
    assert _assess(book=_book(asks=(("0.95", "10"),)), size_shares=100) is None


def test_no_ask_is_no_assessment() -> None:
    book = OrderBook(
        token_id=YES,
        bids=(BookLevel(price=Decimal("0.94"), size=Decimal(10)),),
        asks=(),
        captured_at=NOW,
    )
    assert _assess(book=book) is None


@pytest.mark.parametrize("size", ["0", "-5"])
def test_non_positive_size_is_refused(size: str) -> None:
    assert _assess(size_shares=size) is None


def test_missing_book_for_token_is_refused() -> None:
    other = ClobTokenId("999")
    engine = EvEngine(Thresholds())
    assert (
        engine.assess(
            estimate=ProbabilityEstimate(
                token_id=other,
                model_probability=Decimal("0.9"),
                calibrated_probability=Decimal("0.9"),
                uncertainty=Decimal(0),
                engine="test",
            ),
            snapshot=_snapshot(_book()),
            token_id=other,
            size_shares=Decimal(10),
        )
        is None
    )


# --- The sign that decides ------------------------------------------------
def test_a_positive_edge_smaller_than_costs_is_not_a_trade() -> None:
    """The whole point of the module: edge is the headline, net_ev decides."""
    result = _assess(estimate=_estimate(probability="0.9505"))
    assert result is not None
    assert result.edge > 0
    assert result.net_ev < 0
    assert result.is_positive is False


def test_uncertainty_is_charged_as_a_cost() -> None:
    """A model that admits it does not know is charged for saying so."""
    tight = _assess(estimate=_estimate(uncertainty="0.0"))
    loose = _assess(estimate=_estimate(uncertainty="0.5"))
    assert tight is not None and loose is not None
    assert loose.costs.uncertainty_buffer_bps > tight.costs.uncertainty_buffer_bps
    assert loose.net_ev < tight.net_ev


# --- Confidence -----------------------------------------------------------
def test_inconsistent_data_yields_zero_confidence() -> None:
    """An inconsistent book means our folded state is wrong.

    Discounted rather than zeroed, a confident number computed from a book we know
    to be wrong reaches the gate as merely less attractive.
    """
    result = _assess(snapshot=_snapshot(_book(), DataQuality.INCONSISTENT))
    assert result is not None
    assert result.confidence == 0


def test_confidence_falls_with_staleness() -> None:
    fresh = _assess(snapshot=_snapshot(_book(), DataQuality.FRESH))
    stale = _assess(snapshot=_snapshot(_book(), DataQuality.STALE))
    assert fresh is not None and stale is not None
    assert fresh.confidence > stale.confidence


def test_confidence_falls_with_uncertainty() -> None:
    tight = _assess(estimate=_estimate(uncertainty="0.0"))
    loose = _assess(estimate=_estimate(uncertainty="0.4"))
    assert tight is not None and loose is not None
    assert tight.confidence > loose.confidence


def test_spread_term_is_omitted_rather_than_assumed() -> None:
    """No strategy limit given means the term drops out, not defaults.

    How wide is too wide is a strategy judgement; the EV engine inventing one
    would apply a limit nobody chose.
    """
    without = _assess()
    with_limit = _assess(max_spread_bps=Decimal(150))
    assert without is not None and with_limit is not None
    assert without.confidence > with_limit.confidence


def test_a_book_wider_than_the_strategy_limit_has_no_confidence() -> None:
    wide = _book(bid="0.50", asks=(("0.95", "1000"),))
    result = _assess(book=wide, max_spread_bps=Decimal(10))
    assert result is not None
    assert result.confidence == 0


# --- The shared walk ------------------------------------------------------
def test_vwap_to_fill_refuses_a_partial_walk() -> None:
    book = _book(asks=(("0.95", "10"),))
    assert book.vwap_to_fill(Decimal(100), side=OrderSide.BUY) is None
    assert book.vwap_to_fill(Decimal(10), side=OrderSide.BUY) == Decimal("0.95")


def test_vwap_to_fill_walks_the_right_side() -> None:
    """A BUY sweeps asks upward; a SELL sweeps bids downward."""
    book = OrderBook(
        token_id=YES,
        bids=(
            BookLevel(price=Decimal("0.94"), size=Decimal(10)),
            BookLevel(price=Decimal("0.93"), size=Decimal(10)),
        ),
        asks=(
            BookLevel(price=Decimal("0.95"), size=Decimal(10)),
            BookLevel(price=Decimal("0.96"), size=Decimal(10)),
        ),
        captured_at=NOW,
    )
    assert book.vwap_to_fill(Decimal(20), side=OrderSide.BUY) == Decimal("0.955")
    assert book.vwap_to_fill(Decimal(20), side=OrderSide.SELL) == Decimal("0.935")
