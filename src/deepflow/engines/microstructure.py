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

from deepflow.config.thresholds import Thresholds
from deepflow.core.domain import MarketSnapshot, Microstructure
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

    def __init__(self, thresholds: Thresholds) -> None:
        self._thresholds = thresholds

    def assess(
        self, *, snapshot: MarketSnapshot, token_id: ClobTokenId, size_shares: Decimal
    ) -> FlowAssessment:
        """Combine book imbalance, signed trade flow and liquidity concentration
        into a direction, a score and an execution verdict.

        Returns ``UNKNOWN`` rather than ``NEUTRAL`` when inputs are missing: "no
        information" and "balanced" are different claims, and collapsing them lets a
        market with no measurable flow pass a confirmation gate as though flow had
        been checked and found unobjectionable.

        The score is signed and roughly -1..1, and it is **not** a probability. Flow
        says something about near-term execution conditions and about what other
        participants are doing; it says nothing about whether a team holds a
        two-goal lead. Treating it as a probability is how a system ends up chasing
        moves it caused.
        """
        micro = self._thresholds.microstructure
        book = snapshot.book_for(token_id)
        if book is None:
            return FlowAssessment(
                direction=FlowDirection.UNKNOWN,
                score=Decimal(0),
                liquidity_ok=False,
                notes=("no book for token",),
            )

        features = snapshot.microstructure
        notes: list[str] = []

        imbalance = features.book_imbalance
        flow = features.flow_imbalance
        if imbalance is None and flow is None:
            # Both measures absent. The book may be one-sided, too thin to measure,
            # or the imbalance may have failed its own robustness check.
            return FlowAssessment(
                direction=FlowDirection.UNKNOWN,
                score=Decimal(0),
                liquidity_ok=self._liquidity_ok(features, notes),
                estimated_slippage_bps=features.estimated_slippage_bps,
                notes=(*notes, "neither book nor flow imbalance measurable"),
            )

        # Book imbalance is resting intent and can be withdrawn; traded flow already
        # happened. Weighting them equally would let a wall of cancellable orders
        # outvote actual transactions, so flow carries the larger share when both
        # are present.
        parts: list[tuple[Decimal, Decimal]] = []
        if imbalance is not None:
            parts.append((imbalance, Decimal("0.4")))
            notes.append(f"book imbalance {imbalance:+.3f} within {micro.depth_band}")
        if flow is not None:
            parts.append((flow, Decimal("0.6")))
            notes.append(f"flow imbalance {flow:+.3f}")

        weight = sum((w for _, w in parts), Decimal(0))
        score = sum((value * w for value, w in parts), Decimal(0)) / weight

        return FlowAssessment(
            direction=self._direction(score),
            score=score,
            liquidity_ok=self._liquidity_ok(features, notes),
            estimated_slippage_bps=features.estimated_slippage_bps,
            notes=tuple(notes),
        )

    def _direction(self, score: Decimal) -> FlowDirection:
        micro = self._thresholds.microstructure
        if score >= micro.strong_imbalance:
            return FlowDirection.STRONG_BUY
        if score >= micro.weak_imbalance:
            return FlowDirection.BUY
        if score <= -micro.strong_imbalance:
            return FlowDirection.STRONG_SELL
        if score <= -micro.weak_imbalance:
            return FlowDirection.SELL
        return FlowDirection.NEUTRAL

    def _liquidity_ok(self, features: Microstructure, notes: list[str]) -> bool:
        """Whether the book can be traded against, as opposed to merely quoted.

        Concentration is the check that matters and the one a depth total hides: a
        book whose banded depth sits almost entirely at one level is one
        cancellation from empty, however large that total looks.

        Unknown concentration reads as not-ok. The alternative -- assuming a book is
        fine because we could not measure it -- is the failure this whole module is
        supposed to prevent.
        """
        micro = self._thresholds.microstructure
        concentration = features.liquidity_concentration
        if concentration is None:
            notes.append("liquidity concentration not measurable")
            return False
        if concentration > micro.concentration_warning:
            notes.append(f"depth {concentration:.0%} concentrated at one level")
            return False
        return True
