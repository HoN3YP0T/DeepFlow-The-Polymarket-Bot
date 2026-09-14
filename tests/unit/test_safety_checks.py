"""The 18 real safety checks, and the property that matters most about them.

That property is **fail-closed**. A gate whose checks pass because nothing was
supplied is worse than no gate at all, because the journal then records that the
checklist ran. So the first test here builds a completely empty context and asserts
that every check refuses it and names what was missing.

The rest cover each check's own failure, because a check that cannot fail is
decoration.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.config.thresholds import ProbabilityBand
from deepflow.core.domain import (
    BookLevel,
    Classification,
    CostBreakdown,
    EvAssessment,
    FeeSchedule,
    Market,
    MarketSnapshot,
    OrderBook,
    Outcome,
    ProbabilityEstimate,
    ResolutionCriteria,
)
from deepflow.core.enums import (
    DataQuality,
    MarketCategory,
    OutcomeSide,
    ResolutionValidity,
)
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.risk.limits import RiskVerdict
from deepflow.risk.safety_gate import (
    DEFAULT_CHECKS,
    CheckId,
    GateContext,
    check_data_fresh,
    check_probability_in_band,
    default_gate,
)

YES = ClobTokenId("1")
CID = ConditionId("0xabc")
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _book(*, bid: str = "0.94", ask: str = "0.95", size: str = "1000") -> OrderBook:
    return OrderBook(
        token_id=YES,
        bids=(BookLevel(price=Decimal(bid), size=Decimal(size)),),
        asks=(BookLevel(price=Decimal(ask), size=Decimal(size)),),
        captured_at=NOW,
    )


def _approving_context(**overrides: object) -> GateContext:
    """A context in which all 18 checks pass. Every test below breaks exactly one."""
    ctx = GateContext(
        now=NOW,
        market=Market(
            condition_id=CID,
            question="Will it?",
            outcomes=(Outcome(token_id=YES, label="Yes", side=OutcomeSide.YES),),
            active=True,
            closed=False,
            accepting_orders=True,
            enable_order_book=True,
            fees_enabled=True,
            fee_schedule=FeeSchedule(rate=Decimal("0.04")),
        ),
        classification=Classification(
            category=MarketCategory.FOOTBALL,
            confidence=Decimal("0.95"),
            rationale="tag",
        ),
        resolution=ResolutionCriteria(validity=ResolutionValidity.VALID),
        candidate_band=ProbabilityBand(low=Decimal("0.85"), high=Decimal("0.98")),
        snapshot=MarketSnapshot(
            condition_id=CID,
            books=(_book(),),
            captured_at=NOW,
            quality=DataQuality.FRESH,
        ),
        estimate=ProbabilityEstimate(
            token_id=YES,
            model_probability=Decimal("0.98"),
            calibrated_probability=Decimal("0.98"),
            uncertainty=Decimal("0.01"),
            engine="test-engine",
        ),
        ev=EvAssessment(
            token_id=YES,
            market_probability=Decimal("0.95"),
            model_probability=Decimal("0.98"),
            edge=Decimal("0.03"),
            costs=CostBreakdown(fee_bps=Decimal(20), slippage_bps=Decimal(5)),
            fill_probability=Decimal(1),
            net_ev=Decimal("0.0276"),
            confidence=85,
        ),
        risk=RiskVerdict(approved=True, sizing=None, reason="ok"),
        exposure_breach=None,
        capital_available_usdc=Decimal(1000),
        stake_usdc=Decimal(95),
        duplicate_order_exists=False,
        execution_healthy=True,
        max_data_age_seconds=5.0,
        max_spread_bps=Decimal(150),
        max_slippage_bps=Decimal(100),
        min_liquidity_usdc=Decimal(100),
        min_confidence=70,
        contest_start=None,
        no_entry_seconds_before_start=60.0,
        model_reference_feed=None,
        settlement_reference_feed=None,
    )
    for key, value in overrides.items():
        setattr(ctx, key, value)
    return ctx


# --- The property that matters --------------------------------------------
def test_an_empty_context_fails_every_check() -> None:
    """Nothing supplied must approve nothing.

    The alternative -- checks that pass for want of an input -- produces a journal
    entry saying the checklist ran and approved, which is the most expensive
    possible way for this module to be wrong.
    """
    decision = default_gate().evaluate(GateContext())
    assert not decision.approved

    failed = {r.check for r in decision.failures}
    exempt = {
        # Absent means "no limit breached", which the exposure tracker reports by
        # returning nothing. Its own absence is caught by RISK_APPROVED.
        CheckId.EXPOSURE_ACCEPTABLE,
        # No contest means no book-clearing event to avoid.
        CheckId.BOOK_CLEARED_AT_START,
        # No reference feed means the market does not settle against a price.
        CheckId.REFERENCE_FEED_MATCHED,
    }
    assert failed == set(CheckId) - exempt

    # And every failure says what was missing rather than just "failed".
    assert all(r.detail for r in decision.failures)


def test_every_check_id_is_registered_in_the_default_gate() -> None:
    """An unregistered check fails closed -- but silently narrows the checklist."""
    assert {check for check, _ in DEFAULT_CHECKS} == set(CheckId)
    assert len(DEFAULT_CHECKS) == len(CheckId)


def test_the_approving_context_approves() -> None:
    """The baseline every other test perturbs. If this drifts, they all lie."""
    decision = default_gate().evaluate(_approving_context())
    assert decision.approved, decision.reason


# --- Each check's own failure ---------------------------------------------
@pytest.mark.parametrize(
    ("override", "expected"),
    [
        ({"market": None}, CheckId.MARKET_VALID),
        ({"resolution": None}, CheckId.RESOLUTION_VALID),
        (
            {"resolution": ResolutionCriteria(validity=ResolutionValidity.AMBIGUOUS)},
            CheckId.RESOLUTION_VALID,
        ),
        ({"classification": None}, CheckId.CLASSIFICATION_VALID),
        ({"snapshot": None}, CheckId.DATA_FRESH),
        ({"estimate": None}, CheckId.MODEL_AVAILABLE),
        ({"ev": None}, CheckId.POSITIVE_NET_EV),
        ({"risk": None}, CheckId.RISK_APPROVED),
        (
            {"risk": RiskVerdict(approved=False, sizing=None, reason="drawdown")},
            CheckId.RISK_APPROVED,
        ),
        ({"exposure_breach": "per-event cap"}, CheckId.EXPOSURE_ACCEPTABLE),
        ({"duplicate_order_exists": True}, CheckId.NO_DUPLICATE_ORDER),
        ({"duplicate_order_exists": None}, CheckId.NO_DUPLICATE_ORDER),
        ({"execution_healthy": False}, CheckId.EXECUTION_HEALTHY),
        ({"execution_healthy": None}, CheckId.EXECUTION_HEALTHY),
        ({"capital_available_usdc": Decimal(10)}, CheckId.CAPITAL_AVAILABLE),
        ({"stake_usdc": Decimal(0)}, CheckId.CAPITAL_AVAILABLE),
        ({"min_liquidity_usdc": Decimal(10_000_000)}, CheckId.LIQUIDITY_SUFFICIENT),
        ({"max_spread_bps": Decimal(1)}, CheckId.SPREAD_ACCEPTABLE),
        ({"max_slippage_bps": Decimal(1)}, CheckId.SLIPPAGE_ACCEPTABLE),
    ],
)
def test_one_broken_input_blocks_and_names_itself(
    override: dict[str, object], expected: CheckId
) -> None:
    decision = default_gate().evaluate(_approving_context(**override))
    assert not decision.approved
    assert expected in {r.check for r in decision.blocking_failures}


# --- Checks whose reasoning is worth asserting directly -------------------
def test_a_delayed_market_is_refused() -> None:
    """A delayed market accepts an order and does not match it.

    Every entry then outlives its timeout and every fill arrives as a surprise, so
    this is a property of the market rather than a configuration preference.
    """
    market = _approving_context().market
    assert market is not None
    decision = default_gate().evaluate(
        _approving_context(market=market.model_copy(update={"seconds_delay": 1}))
    )
    assert not decision.approved
    assert "seconds_delay=1" in decision.reason


def test_inconsistent_data_is_refused_distinctly_from_stale() -> None:
    """No amount of waiting fixes an inconsistent book; it must be re-anchored."""
    snapshot = _approving_context().snapshot
    assert snapshot is not None
    inconsistent = default_gate().evaluate(
        _approving_context(
            snapshot=snapshot.model_copy(update={"quality": DataQuality.INCONSISTENT})
        )
    )
    assert "inconsistent" in inconsistent.reason.lower()

    stale = default_gate().evaluate(
        _approving_context(snapshot=snapshot.model_copy(update={"quality": DataQuality.STALE}))
    )
    assert "STALE" in stale.reason


def test_data_older_than_the_budget_is_refused() -> None:
    snapshot = _approving_context().snapshot
    assert snapshot is not None
    decision = default_gate().evaluate(
        _approving_context(now=NOW + timedelta(seconds=30), max_data_age_seconds=5.0)
    )
    assert not decision.approved
    assert CheckId.DATA_FRESH in {r.check for r in decision.blocking_failures}


def test_a_degenerate_probability_is_refused_not_clamped() -> None:
    """A model certain about a future event has failed, and should say so."""
    for probability in ("0", "1"):
        decision = default_gate().evaluate(
            _approving_context(
                estimate=ProbabilityEstimate(
                    token_id=YES,
                    model_probability=Decimal(probability),
                    calibrated_probability=Decimal(probability),
                    uncertainty=Decimal("0.01"),
                    engine="test-engine",
                )
            )
        )
        assert not decision.approved
        assert "degenerate" in decision.reason


def test_positive_edge_with_negative_net_ev_is_refused() -> None:
    """The distinction the whole module exists to enforce."""
    ev = _approving_context().ev
    assert ev is not None
    decision = default_gate().evaluate(
        _approving_context(ev=ev.model_copy(update={"net_ev": Decimal("-0.004")}))
    )
    assert not decision.approved
    assert CheckId.POSITIVE_NET_EV in {r.check for r in decision.blocking_failures}


def test_other_sports_is_refused_as_having_no_model() -> None:
    """Knowing it is a sport is not knowing how to price it."""
    decision = default_gate().evaluate(
        _approving_context(
            classification=Classification(
                category=MarketCategory.OTHER_SPORTS,
                confidence=Decimal("0.95"),
                rationale="generic sports tag",
            )
        )
    )
    assert not decision.approved
    assert "OTHER_SPORTS" in decision.reason


# --- Finding 64: the venue clears the book at contest start ---------------
def test_entry_just_before_a_contest_start_is_refused() -> None:
    """The venue empties the book at the start, and may be late doing it.

    Entering inside that window bets on the venue's timing rather than on the
    market.
    """
    decision = default_gate().evaluate(
        _approving_context(contest_start=NOW + timedelta(seconds=30))
    )
    assert not decision.approved
    assert "no-entry window" in decision.reason


def test_entry_well_before_a_contest_start_is_allowed() -> None:
    decision = default_gate().evaluate(
        _approving_context(contest_start=NOW + timedelta(minutes=30))
    )
    assert decision.approved, decision.reason


def test_a_known_start_with_no_clock_is_refused() -> None:
    """ "Is it near the start" is unanswerable without a clock, so it is not answered."""
    decision = default_gate().evaluate(
        _approving_context(contest_start=NOW + timedelta(seconds=30), now=None)
    )
    assert not decision.approved


# --- Finding 63: the feed we model must be the feed that settles ----------
def test_modelling_spot_for_a_twap_settled_market_is_refused() -> None:
    """Not slightly wrong -- a different instrument.

    Crypto up/down settles on a Chainlink TWAP with a 30-second lookback. At a
    five-minute horizon the spot-to-TWAP basis is the entire edge.
    """
    decision = default_gate().evaluate(
        _approving_context(
            model_reference_feed="binance_spot",
            settlement_reference_feed="chainlink_twap_30s",
        )
    )
    assert not decision.approved
    assert "settles on" in decision.reason


def test_matching_feeds_are_allowed() -> None:
    decision = default_gate().evaluate(
        _approving_context(
            model_reference_feed="chainlink_twap_30s",
            settlement_reference_feed="chainlink_twap_30s",
        )
    )
    assert decision.approved, decision.reason


@pytest.mark.parametrize(
    ("modelled", "settles"),
    [("binance_spot", None), (None, "chainlink_twap_30s")],
)
def test_one_sided_feed_knowledge_is_refused(modelled: str | None, settles: str | None) -> None:
    """An asymmetry means somebody was never asked."""
    decision = default_gate().evaluate(
        _approving_context(model_reference_feed=modelled, settlement_reference_feed=settles)
    )
    assert not decision.approved
    assert CheckId.REFERENCE_FEED_MATCHED in {r.check for r in decision.blocking_failures}


# --- The evidence trail ---------------------------------------------------
def test_all_failures_are_reported_not_just_the_first() -> None:
    """Four failures rather than one tells you the gate is mis-tuned, not unlucky."""
    decision = default_gate().evaluate(
        _approving_context(
            market=None,
            resolution=None,
            execution_healthy=False,
            duplicate_order_exists=True,
        )
    )
    assert len(decision.blocking_failures) >= 4
    assert decision.reason.count(";") >= 3


def test_degraded_data_cannot_open_a_new_entry() -> None:
    """**The invariant that was written twice and enforced nowhere.**

    "Only FRESH data may open new risk" is stated on
    :attr:`MarketSnapshot.entries_allowed` and in ``FeatureEngine.assess_snapshot``'s
    docstring. Until 2026-09-14 ``entries_allowed`` had no callers anywhere in the
    codebase and this check tested only for INCONSISTENT and STALE, so DEGRADED passed.

    DEGRADED is not a stale price, it is a possibly-wrong one: the stream marked a gap
    after a reconnect or a dropped update, so the folded book may be missing a level
    change that has already happened. In the 0.85-0.98 band one missed level is most of
    the edge.
    """
    degraded = _approving_context(
        snapshot=MarketSnapshot(
            condition_id=CID,
            books=(_book(),),
            captured_at=NOW,
            quality=DataQuality.DEGRADED,
        )
    )
    result = check_data_fresh(degraded)
    assert not result.passed
    assert "DEGRADED" in result.detail


def test_the_freshness_check_reads_the_domain_rule_rather_than_restating_it() -> None:
    """One definition of "may this open risk", so the check cannot drift from it."""
    fresh = MarketSnapshot(
        condition_id=CID, books=(_book(),), captured_at=NOW, quality=DataQuality.FRESH
    )
    assert fresh.entries_allowed
    for quality in (DataQuality.DEGRADED, DataQuality.STALE, DataQuality.INCONSISTENT):
        snapshot = MarketSnapshot(
            condition_id=CID, books=(_book(),), captured_at=NOW, quality=quality
        )
        assert not snapshot.entries_allowed
        assert not check_data_fresh(_approving_context(snapshot=snapshot)).passed


def test_fresh_data_still_passes() -> None:
    """The check has to be able to succeed, or it is not a gate but a wall."""
    assert check_data_fresh(_approving_context()).passed


def test_an_estimate_outside_the_candidate_band_is_refused() -> None:
    """**The check that would have stopped a fabricated football edge.**

    Live fixture, market at 0.79 on the home side, model at 0.42 -- so the model
    claimed a +0.17 edge on the away side at 0.12. The edge was entirely the size of
    the model's own blind spot: it has no team ratings, because the venue's feed sends
    none, so a big club against a small one prices identically to two equal sides.

    Nothing else stops it. The uncertainty buffer charges about 176 bps at that price
    against an edge worth about 14,575.
    """
    outside = _approving_context(
        estimate=ProbabilityEstimate(
            token_id=YES,
            model_probability=Decimal("0.295"),
            calibrated_probability=Decimal("0.295"),
            uncertainty=Decimal("0.21"),
            engine="football",
        )
    )
    result = check_probability_in_band(outside)
    assert not result.passed
    assert "outside candidate band" in result.detail


def test_a_missing_band_fails_closed() -> None:
    """A strategy that did not state its band has not said this is a trade it wants,
    and silence is not consent."""
    assert not check_probability_in_band(_approving_context(candidate_band=None)).passed


def test_the_band_check_reads_the_calibrated_probability() -> None:
    """Sizing uses the calibrated number, and a fitted curve exists precisely to move
    one relative to the other -- so gating the raw output would gate something the
    trade is not made on."""
    shifted = _approving_context(
        estimate=ProbabilityEstimate(
            token_id=YES,
            # Raw is outside the band; calibrated is inside it. The calibrated one wins.
            model_probability=Decimal("0.995"),
            calibrated_probability=Decimal("0.95"),
            uncertainty=Decimal("0.02"),
            engine="test-engine",
        )
    )
    assert check_probability_in_band(shifted).passed
