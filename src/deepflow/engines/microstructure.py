"""Market-microstructure signals.

Order-flow evidence, kept separate from the probability engines. Flow says
something about what other participants believe and about near-term execution
conditions; it says nothing about whether a team will hold a two-goal lead.

Used as a confirmation gate and an execution input, not as a probability
source. Treating flow as a probability is how a system ends up chasing moves it
caused.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from deepflow.core.domain import MarketSnapshot
from deepflow.core.types import ClobTokenId


class FlowDirection(StrEnum):
    STRONG_BUY = "STRONG_BUY"
    BUY = "BUY"
    NEUTRAL = "NEUTRAL"
    SELL = "SELL"
    STRONG_SELL = "STRONG_SELL"
    UNKNOWN = "UNKNOWN"


class FlowAssessment(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: FlowDirection
    score: Decimal
    """Signed, roughly -1..1."""
    liquidity_ok: bool
    estimated_slippage_bps: Decimal | None = None
    notes: tuple[str, ...] = ()


class MicrostructureEngine:
    """Scores order flow and execution conditions."""

    def assess(
        self, *, snapshot: MarketSnapshot, token_id: ClobTokenId, size_shares: Decimal
    ) -> FlowAssessment:
        """TODO(skeleton): combine book imbalance, signed trade flow and
        liquidity concentration into a direction and score; estimate slippage
        for the intended size. Returns UNKNOWN rather than NEUTRAL when inputs
        are missing -- "no information" and "balanced" are different claims."""
        raise NotImplementedError("MicrostructureEngine.assess")
