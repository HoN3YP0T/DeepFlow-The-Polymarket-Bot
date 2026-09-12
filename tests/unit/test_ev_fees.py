"""Fee derivation inside the EV engine.

`CostBreakdown.fee_bps` defaulted to zero in the scaffold and nothing populated
it. These tests hold the wiring in place, because a zero fee is not a small error
in the 0.85-0.98 band -- it is a large fraction of the whole edge the gates are
sized around.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from deepflow.config.thresholds import ProbabilityBand, StrategyThresholds
from deepflow.core.domain import FeeSchedule, Market, Outcome
from deepflow.core.enums import OutcomeSide
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.engines.ev import EvEngine

YES = ClobTokenId("1")
NO = ClobTokenId("2")


_DEFAULT = FeeSchedule(rate=Decimal("0.04"))


def _market(*, fees_enabled: bool = True, schedule: FeeSchedule | None = _DEFAULT) -> Market:
    if not fees_enabled:
        schedule = None
    return Market(
        condition_id=ConditionId("0xabc"),
        question="Will it?",
        outcomes=(
            Outcome(token_id=YES, label="Yes", side=OutcomeSide.YES),
            Outcome(token_id=NO, label="No", side=OutcomeSide.NO),
        ),
        active=True,
        closed=False,
        accepting_orders=True,
        end_date=datetime(2027, 1, 1, tzinfo=UTC),
        fees_enabled=fees_enabled,
        fee_schedule=schedule,
    )


def test_fee_is_taken_from_the_market_not_the_category() -> None:
    """The per-category table is a planning fallback; the market is truth. A
    recategorised market must price off its own schedule."""
    market = _market(schedule=FeeSchedule(rate=Decimal("0.07")))
    cheap = _market(schedule=FeeSchedule(rate=Decimal("0.04")))
    price = Decimal("0.90")
    assert EvEngine.fee_bps(market, price=price) > EvEngine.fee_bps(cheap, price=price)


def test_fee_free_market_costs_nothing() -> None:
    """Geopolitics markets are documented as fee-free."""
    assert EvEngine.fee_bps(_market(fees_enabled=False), price=Decimal("0.9")) == 0


def test_missing_schedule_yields_no_fee_and_must_be_gated_upstream() -> None:
    """A market flagged ``fees_enabled`` with no schedule returns zero here.

    That is why ``fail_closed_on_unknown_fee_schedule`` exists: this function
    cannot invent a rate, so the gate has to refuse the trade rather than let a
    silent zero flatter net EV.
    """
    market = _market(fees_enabled=True, schedule=None)
    assert EvEngine.fee_bps(market, price=Decimal("0.9")) == 0
    assert (
        StrategyThresholds(
            candidate_band=ProbabilityBand(low=Decimal("0.85"), high=Decimal("0.98"))
        ).min_net_ev
        > 0
    )


def test_fee_in_bps_is_material_against_the_minimum_edge() -> None:
    """At 0.95 with a 4% rate the fee is ~20 bps, against a default
    ``min_net_ev`` of 0.005 (50 bps). Omitting it consumes 40% of the minimum
    acceptable edge, which is enough to flip marginal trades."""
    fee = EvEngine.fee_bps(_market(), price=Decimal("0.95"))
    assert Decimal(15) < fee < Decimal(25)


def test_fee_bps_rises_as_price_falls() -> None:
    market = _market()
    prices = [Decimal("0.98"), Decimal("0.90"), Decimal("0.70"), Decimal("0.50")]
    fees = [EvEngine.fee_bps(market, price=p) for p in prices]
    assert fees == sorted(fees)


def test_fee_bps_handles_a_degenerate_price() -> None:
    market = _market()
    assert EvEngine.fee_bps(market, price=Decimal(0)) == 0
    assert EvEngine.fee_bps(market, price=Decimal(1)) == 0
