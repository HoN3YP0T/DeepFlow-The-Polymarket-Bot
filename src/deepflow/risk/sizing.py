"""Position sizing. Section 17.

Capped fractional Kelly. Full Kelly is optimal only if the probability is
exactly right; ours is an estimate, and Kelly's downside for an overestimated
edge is severe and asymmetric -- overbetting by 2x does not halve growth, it
can drive it negative. So the raw fraction is scaled down, haircut for the
estimate's own uncertainty, and then hard-capped.

Unlimited Kelly is never used.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from deepflow.config.thresholds import RiskLimits
from deepflow.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class SizingResult:
    """Outcome of a sizing calculation, with the binding constraint named."""

    stake_usdc: Decimal
    kelly_fraction_raw: Decimal
    kelly_fraction_applied: Decimal
    binding_constraint: str
    """Which limit actually determined the size. Recorded in the journal so
    a persistently tiny size can be traced to the rule causing it."""


def kelly_fraction(*, probability: Decimal, price: Decimal) -> Decimal:
    """Full-Kelly fraction for a binary contract.

    A contract bought at ``price`` pays 1 on success, 0 otherwise, so net odds
    are ``b = (1 - price) / price`` and Kelly is ``(p * b - (1 - p)) / b``,
    which reduces to ``(p - price) / (1 - price)``.

    Returns 0 for a non-positive edge -- Kelly's negative branch means "take
    the other side", which is a different trade with its own gates, not a
    smaller version of this one.
    """
    if not (0 < price < 1):
        raise ValueError(f"price must be in (0, 1), got {price}")
    if not (0 <= probability <= 1):
        raise ValueError(f"probability must be in [0, 1], got {probability}")

    edge = probability - price
    if edge <= 0:
        return Decimal(0)
    return edge / (1 - price)


def size_position(
    *,
    probability: Decimal,
    price: Decimal,
    uncertainty: Decimal,
    bankroll: Decimal,
    limits: RiskLimits,
    available_capital: Decimal,
) -> SizingResult:
    """Size a position under every applicable cap.

    Order of operations, each step only ever shrinking the number:

    1. full Kelly from probability and price
    2. haircut for estimate uncertainty -- a probability held loosely is
       effectively a smaller edge, and this is where an overconfident model
       gets absorbed instead of compounding into an oversized position
    3. scale by ``kelly_fraction`` (fractional Kelly)
    4. clamp to ``kelly_hard_cap``
    5. clamp to the per-position fraction and absolute cap
    6. clamp to capital actually available after reserves

    ``binding_constraint`` names whichever step won.
    """
    raw = kelly_fraction(probability=probability, price=price)
    if raw <= 0:
        return SizingResult(
            stake_usdc=Decimal(0),
            kelly_fraction_raw=raw,
            kelly_fraction_applied=Decimal(0),
            binding_constraint="no_edge",
        )

    # A 1-sigma band of u shrinks the actionable edge; clamp at zero so a
    # wildly uncertain estimate sizes to nothing rather than flipping sign.
    haircut = max(Decimal(0), Decimal(1) - uncertainty)
    applied = raw * haircut
    constraint = "kelly_uncertainty_haircut"

    scaled = applied * limits.kelly_fraction
    if scaled < applied:
        constraint = "fractional_kelly"
    applied = scaled

    if applied > limits.kelly_hard_cap:
        applied = limits.kelly_hard_cap
        constraint = "kelly_hard_cap"

    if applied > limits.max_position_fraction:
        applied = limits.max_position_fraction
        constraint = "max_position_fraction"

    stake = bankroll * applied

    if stake > limits.max_position_usdc:
        stake = limits.max_position_usdc
        constraint = "max_position_usdc"

    if stake > available_capital:
        stake = max(Decimal(0), available_capital)
        constraint = "available_capital"

    return SizingResult(
        stake_usdc=stake,
        kelly_fraction_raw=raw,
        kelly_fraction_applied=applied,
        binding_constraint=constraint,
    )
