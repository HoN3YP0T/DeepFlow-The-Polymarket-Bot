"""Exit engine: the noise-versus-state-change distinction, which is the whole job.

Two failures this suite exists to prevent, and they pull in opposite directions:

* Exiting on noise. At the prices this system trades (0.85-0.98) the spread is a large
  fraction of the entire edge, so paying it twice on a wobble converts a positive-EV
  trade into a realized loss — repeatedly, which is how a profitable strategy bleeds out.
* Holding through a state change. A goal against the position means the entry
  probability is now irrelevant, and there is no band small enough to make that a
  hold.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from deepflow.config.thresholds import ExitThresholds
from deepflow.core.domain import (
    BookLevel,
    MarketSnapshot,
    Microstructure,
    OrderBook,
    Position,
    ProbabilityEstimate,
    SmartMoneyEntry,
    SmartMoneySignal,
)
from deepflow.core.enums import ExitAction, OrderSide
from deepflow.core.types import ClobTokenId, ConditionId, PositionId, WalletAddress
from deepflow.positions.exit_engine import ExitEngine, ExitSignals, StateChange

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
TOKEN = ClobTokenId("11111")
CONDITION = ConditionId("0xcond")


def _position(*, entry: str = "0.94", shares: str = "100") -> Position:
    return Position(
        position_id=PositionId("p1"),
        condition_id=CONDITION,
        token_id=TOKEN,
        shares=Decimal(shares),
        average_entry_price=Decimal(entry),
        entry_probability=Decimal(entry),
        opened_at=NOW,
    )


def _snapshot(
    *,
    bid: str | None = "0.93",
    ask: str = "0.96",
    velocity: str | None = None,
    flow: str | None = None,
    remaining: int | None = None,
) -> MarketSnapshot:
    bids = (BookLevel(price=Decimal(bid), size=Decimal(500)),) if bid is not None else ()
    return MarketSnapshot(
        condition_id=CONDITION,
        books=(
            OrderBook(
                token_id=TOKEN,
                bids=bids,
                asks=(BookLevel(price=Decimal(ask), size=Decimal(500)),),
                captured_at=NOW,
            ),
        ),
        microstructure=Microstructure(
            price_velocity=Decimal(velocity) if velocity is not None else None,
            flow_imbalance=Decimal(flow) if flow is not None else None,
        ),
        time_remaining_seconds=remaining,
        captured_at=NOW,
    )


def _estimate(probability: str) -> ProbabilityEstimate:
    return ProbabilityEstimate(
        token_id=TOKEN,
        model_probability=Decimal(probability),
        calibrated_probability=Decimal(probability),
        uncertainty=Decimal("0.05"),
        engine="test",
    )


def _exiting_whale() -> SmartMoneySignal:
    return SmartMoneySignal(
        condition_id=CONDITION,
        score=80,
        exits=(
            SmartMoneyEntry(
                wallet=WalletAddress("0xwhale"),
                side=OrderSide.SELL,
                outcome_label="Yes",
                notional_usdc=Decimal(5000),
                entry_price=Decimal("0.9"),
                market_probability_at_entry=Decimal("0.9"),
                observed_at=NOW,
                is_exit=True,
            ),
        ),
    )


async def _decide(engine: ExitEngine, **kwargs: object) -> object:
    return await engine.evaluate(
        position=kwargs.get("position") or _position(),  # type: ignore[arg-type]
        snapshot=kwargs.get("snapshot") or _snapshot(),  # type: ignore[arg-type]
        estimate=kwargs.get("estimate"),  # type: ignore[arg-type]
        smart_money=kwargs.get("smart_money"),  # type: ignore[arg-type]
    )


# --- Noise ----------------------------------------------------------------
@pytest.mark.asyncio
async def test_a_small_adverse_move_is_noise_and_holds() -> None:
    """94 -> 93 on a thin outcome book is one impatient seller, not a changed position."""
    decision = await _decide(ExitEngine(), snapshot=_snapshot(bid="0.93"))
    assert decision.action is ExitAction.HOLD
    assert "noise band" in decision.reason


@pytest.mark.asyncio
async def test_a_favourable_move_is_never_noise_tested() -> None:
    """The band answers "should we get out", and a move in our favour is not a reason
    to. Testing it symmetrically would exit winners."""
    decision = await _decide(ExitEngine(), snapshot=_snapshot(bid="0.97", ask="0.98"))
    assert decision.action is ExitAction.HOLD


@pytest.mark.asyncio
async def test_an_unmeasured_volatility_falls_back_to_the_configured_band() -> None:
    """Not to zero. Zero would make every tick significant, which is the failure the
    band exists to prevent."""
    engine = ExitEngine(thresholds=ExitThresholds(noise_band=Decimal("0.05")))
    assert engine.noise_band_for(_snapshot(velocity=None)) == Decimal("0.05")


@pytest.mark.asyncio
async def test_the_band_scales_with_the_markets_own_volatility() -> None:
    """A fixed band is too tight on a 5-minute BTC market and far too loose on a
    settled football market."""
    engine = ExitEngine()
    quiet = engine.noise_band_for(_snapshot(velocity="0.005"))
    wild = engine.noise_band_for(_snapshot(velocity="0.04"))
    assert quiet < wild


@pytest.mark.asyncio
async def test_the_band_is_clamped_at_both_ends() -> None:
    """A volatility near zero would make the band vanish; a spike would make it so wide
    that a real state change reads as noise."""
    engine = ExitEngine()
    thresholds = ExitThresholds()
    assert engine.noise_band_for(_snapshot(velocity="0.0000001")) == thresholds.min_noise_band
    assert engine.noise_band_for(_snapshot(velocity="99")) == thresholds.max_noise_band


@pytest.mark.asyncio
async def test_the_band_tightens_close_to_resolution() -> None:
    """The same 2-point drop is noise at 60 minutes and a warning at 60 seconds: there
    is less time left for it to mean-revert."""
    engine = ExitEngine()
    early = engine.noise_band_for(_snapshot(remaining=3600))
    late = engine.noise_band_for(_snapshot(remaining=30))
    assert late < early


# --- State change ---------------------------------------------------------
def test_only_a_state_change_can_reach_an_emergency_exit() -> None:
    """Pinned as a structural fact: every other field in ExitSignals is price-derived,
    and none of them may produce an emergency exit however extreme."""
    signals = ExitSignals(
        current_probability=Decimal("0.10"),
        entry_probability=Decimal("0.94"),
        model_probability=Decimal("0.05"),
        probability_velocity=Decimal("-0.5"),
        smart_money_exiting=True,
        opposing_flow=Decimal(1),
        current_net_ev=Decimal("-5"),
    )
    assert not signals.state_change_detected


@pytest.mark.asyncio
async def test_a_state_change_exits_in_full_regardless_of_the_band() -> None:
    """The entry probability is irrelevant once the world has changed, so the move being
    small is not a reason to stay in.

    The same price is used for both halves of this test: it holds without the state
    change and exits with it, which is what proves the bypass rather than merely
    asserting that a big move exits.
    """
    engine = ExitEngine()
    held = await engine.evaluate(
        position=_position(),
        snapshot=_snapshot(bid="0.939"),
        estimate=None,
        smart_money=None,
    )
    assert held.action is ExitAction.HOLD

    exited = await engine.evaluate(
        position=_position(),
        snapshot=_snapshot(bid="0.939"),
        estimate=None,
        smart_money=None,
        state_change=StateChange(detail="goal against"),
    )
    assert exited.action is ExitAction.EMERGENCY_EXIT
    assert exited.fraction == Decimal(1)
    assert exited.exit_score == 100
    assert "goal against" in exited.reason


@pytest.mark.asyncio
async def test_a_state_change_in_our_favour_does_not_exit() -> None:
    """A goal *for* the outcome we hold is a state change too. Exiting on it sells the
    winner, which is why direction is the caller's to supply."""
    decision = await ExitEngine().evaluate(
        position=_position(),
        snapshot=_snapshot(),
        estimate=None,
        smart_money=None,
        state_change=StateChange(detail="goal for", against_position=False),
    )
    assert decision.action is not ExitAction.EMERGENCY_EXIT


@pytest.mark.asyncio
async def test_a_negative_net_ev_reaches_the_engine_through_evaluate() -> None:
    """Wired, not merely representable: the field exists on ExitSignals and evaluate
    must have a way to set it, or the EV veto is unreachable in production."""
    decision = await ExitEngine().evaluate(
        position=_position(),
        snapshot=_snapshot(bid="0.939"),
        estimate=None,
        smart_money=None,
        net_ev=Decimal("-2"),
    )
    assert decision.action is not ExitAction.HOLD
    assert "negative_net_ev" in decision.triggers


# --- Deterioration --------------------------------------------------------
@pytest.mark.asyncio
async def test_a_model_far_below_the_price_exits_in_full() -> None:
    """The model is the only input with a view of its own, so it carries the most
    weight of anything price-derived."""
    decision = await _decide(
        ExitEngine(), snapshot=_snapshot(bid="0.80"), estimate=_estimate("0.30")
    )
    assert decision.action is ExitAction.FULL_EXIT
    assert decision.fraction == Decimal(1)
    assert "model_below_price" in decision.triggers


@pytest.mark.asyncio
async def test_moderate_deterioration_reduces_rather_than_exits() -> None:
    """The middle band has a genuinely different answer: keep the thesis, cut the size."""
    engine = ExitEngine(thresholds=ExitThresholds(partial_exit_score=10, full_exit_score=95))
    decision = await _decide(engine, snapshot=_snapshot(bid="0.85"), estimate=_estimate("0.80"))
    assert decision.action is ExitAction.PARTIAL_EXIT
    assert decision.fraction == ExitThresholds().partial_exit_fraction


@pytest.mark.asyncio
async def test_a_negative_net_ev_bypasses_the_band() -> None:
    """Not a price wobble: at the current price the trade no longer pays after costs,
    and holding it because the move was small is holding a position with no edge."""
    engine = ExitEngine()
    signals = ExitSignals(
        current_probability=Decimal("0.939"),
        entry_probability=Decimal("0.94"),
        model_probability=Decimal("0.94"),
        current_net_ev=Decimal("-1"),
    )
    assert engine._is_noise(signals, volatility=None) is False


@pytest.mark.asyncio
async def test_smart_money_exiting_contributes_but_does_not_decide() -> None:
    """Evidence, not a verdict: a whale leaving is one input among several and must
    clear the band like the rest."""
    decision = await _decide(ExitEngine(), smart_money=_exiting_whale())
    assert decision.action is ExitAction.HOLD
    assert "smart_money_exiting" in decision.triggers


@pytest.mark.asyncio
async def test_the_score_is_recorded_even_when_the_answer_is_hold() -> None:
    """The score goes in the journal, and the held-but-deteriorating set is what shows
    whether the band is calibrated or merely tight."""
    decision = await _decide(ExitEngine(), estimate=_estimate("0.90"), smart_money=_exiting_whale())
    assert decision.action is ExitAction.HOLD
    assert decision.exit_score > 0


# --- Adding ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_adding_needs_the_model_above_the_current_price() -> None:
    """Adding because the price fell is averaging down — the same trade at worse odds
    unless the model still supports it."""
    decision = await _decide(
        ExitEngine(), snapshot=_snapshot(bid="0.90"), estimate=_estimate("0.98")
    )
    assert decision.action is ExitAction.ADD


@pytest.mark.asyncio
async def test_a_flat_model_does_not_add() -> None:
    decision = await _decide(
        ExitEngine(), snapshot=_snapshot(bid="0.93"), estimate=_estimate("0.935")
    )
    assert decision.action is ExitAction.HOLD


@pytest.mark.asyncio
async def test_deterioration_outranks_adding() -> None:
    """Precedence, not arithmetic: a position that is both deteriorating and cheap must
    not be added to, and this ordering is what prevents it."""
    engine = ExitEngine(thresholds=ExitThresholds(partial_exit_score=10, full_exit_score=20))
    decision = await _decide(engine, snapshot=_snapshot(bid="0.70"), estimate=_estimate("0.60"))
    assert decision.action in (ExitAction.FULL_EXIT, ExitAction.PARTIAL_EXIT)


# --- Missing data ---------------------------------------------------------
@pytest.mark.asyncio
async def test_no_bid_holds_rather_than_exits() -> None:
    """An unreadable book is a reason to look again. Selling into one converts a data
    problem into a realized loss at whatever price happens to be quoted."""
    decision = await _decide(ExitEngine(), snapshot=_snapshot(bid=None))
    assert decision.action is ExitAction.HOLD
    assert "no closing price" in decision.reason


@pytest.mark.asyncio
async def test_the_mark_is_the_bid_not_the_mid() -> None:
    """The mid is a price at which nobody has offered to buy anything. Marking there is
    the gap between a position that looks profitable and one that is."""
    engine = ExitEngine()
    decision = await _decide(engine, snapshot=_snapshot(bid="0.90", ask="0.99"))
    # A mid of 0.945 would be *above* the 0.94 entry and read as a gain. The bid of 0.90
    # is a 4-point adverse move, so it clears the 0.03 band and is recorded as one.
    assert "adverse_move" in decision.triggers
    assert "0.04" in decision.reason
