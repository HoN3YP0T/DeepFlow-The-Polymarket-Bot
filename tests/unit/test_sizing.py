"""Sizing tests. Section 17: never unlimited Kelly."""

from __future__ import annotations

from decimal import Decimal

import pytest

from deepflow.config.thresholds import RiskLimits
from deepflow.risk.sizing import kelly_fraction, size_position

D = Decimal


def test_kelly_zero_when_no_edge() -> None:
    assert kelly_fraction(probability=D("0.90"), price=D("0.90")) == 0
    assert kelly_fraction(probability=D("0.85"), price=D("0.90")) == 0


def test_kelly_matches_closed_form() -> None:
    """(p - price) / (1 - price): a 0.95 estimate at 0.90 is 0.05/0.10."""
    assert kelly_fraction(probability=D("0.95"), price=D("0.90")) == D("0.5")


def test_kelly_rejects_out_of_range_inputs() -> None:
    with pytest.raises(ValueError, match="price"):
        kelly_fraction(probability=D("0.9"), price=D(0))
    with pytest.raises(ValueError, match="price"):
        kelly_fraction(probability=D("0.9"), price=D(1))
    with pytest.raises(ValueError, match="probability"):
        kelly_fraction(probability=D("1.5"), price=D("0.9"))


def test_size_is_capped_far_below_full_kelly(limits: RiskLimits) -> None:
    """Full Kelly here is 0.5 of bankroll. Nothing may get close to that."""
    result = size_position(
        probability=D("0.95"),
        price=D("0.90"),
        uncertainty=D("0.02"),
        bankroll=D(100_000),
        limits=limits,
        available_capital=D(100_000),
    )
    assert result.kelly_fraction_raw == D("0.5")
    assert result.kelly_fraction_applied <= limits.kelly_hard_cap
    assert result.stake_usdc <= limits.max_position_usdc


def test_no_edge_sizes_to_zero(limits: RiskLimits) -> None:
    result = size_position(
        probability=D("0.90"),
        price=D("0.94"),
        uncertainty=D("0.01"),
        bankroll=D(10_000),
        limits=limits,
        available_capital=D(10_000),
    )
    assert result.stake_usdc == 0
    assert result.binding_constraint == "no_edge"


def test_high_uncertainty_shrinks_the_stake(limits: RiskLimits) -> None:
    """An overconfident model must be absorbed by the haircut, not compounded
    into a larger position.

    Uses a marginal edge so the Kelly branch is what binds -- on a large edge
    the per-position cap dominates and the haircut is invisible (see
    ``test_position_cap_dominates_kelly_on_large_edges``).
    """
    confident = size_position(
        probability=D("0.9405"),
        price=D("0.94"),
        uncertainty=D("0.01"),
        bankroll=D(1_000),
        limits=limits,
        available_capital=D(1_000),
    )
    unsure = size_position(
        probability=D("0.9405"),
        price=D("0.94"),
        uncertainty=D("0.50"),
        bankroll=D(1_000),
        limits=limits,
        available_capital=D(1_000),
    )
    assert unsure.stake_usdc < confident.stake_usdc
    assert confident.binding_constraint in {"fractional_kelly", "kelly_uncertainty_haircut"}


def test_position_cap_dominates_kelly_on_large_edges(limits: RiskLimits) -> None:
    """Documents the real behaviour of the default limits.

    At a 5c edge, full Kelly is 50% of bankroll. Every cap in the chain fires
    and the per-position fraction (2%) is what actually binds -- Kelly is the
    starting point, not the answer. If this test ever reports a kelly
    constraint, the caps have been loosened to the point where the sizing is
    genuinely Kelly-driven, which is a deliberate decision, not a default.
    """
    result = size_position(
        probability=D("0.95"),
        price=D("0.90"),
        uncertainty=D("0.01"),
        bankroll=D(1_000),
        limits=limits,
        available_capital=D(1_000),
    )
    assert result.kelly_fraction_raw == D("0.5")
    assert result.binding_constraint == "max_position_fraction"
    assert result.stake_usdc == D(1_000) * limits.max_position_fraction


def test_total_uncertainty_sizes_to_nothing(limits: RiskLimits) -> None:
    result = size_position(
        probability=D("0.95"),
        price=D("0.90"),
        uncertainty=D(1),
        bankroll=D(1_000),
        limits=limits,
        available_capital=D(1_000),
    )
    assert result.stake_usdc == 0


def test_available_capital_binds(limits: RiskLimits) -> None:
    result = size_position(
        probability=D("0.95"),
        price=D("0.90"),
        uncertainty=D("0.01"),
        bankroll=D(100_000),
        limits=limits,
        available_capital=D(25),
    )
    assert result.stake_usdc == D(25)
    assert result.binding_constraint == "available_capital"


def test_stake_never_negative(limits: RiskLimits) -> None:
    result = size_position(
        probability=D("0.95"),
        price=D("0.90"),
        uncertainty=D("0.01"),
        bankroll=D(1_000),
        limits=limits,
        available_capital=D(-50),
    )
    assert result.stake_usdc == 0
