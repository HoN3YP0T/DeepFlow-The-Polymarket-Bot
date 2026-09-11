"""Probability engine base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from deepflow.core.domain import (
    Classification,
    Market,
    MarketSnapshot,
    ProbabilityEstimate,
)
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ClobTokenId


class BaseProbabilityEngine(ABC):
    """Common scaffolding for probability engines.

    Two contracts every subclass must honour:

    1. **Independence.** Derive the probability from state -- score, clock,
       strike distance, evidence -- never from the market price. An engine that
       anchors on the price produces an edge that is an artefact of its own
       input, and it will look most confident exactly when it is least useful.

    2. **Abstention.** Return ``None`` when the state needed is missing or
       inconsistent. Abstaining costs one skipped trade; inventing a number
       puts a fabricated probability into sizing.
    """

    name: str = "base"
    categories: frozenset[MarketCategory] = frozenset()

    def supports(self, classification: Classification) -> bool:
        return classification.is_tradeable and classification.category in self.categories

    @abstractmethod
    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """Estimate P(outcome) for ``token_id``, or ``None`` to abstain."""

    def _calibrate(self, raw: Decimal) -> Decimal:
        """Map a raw model output onto a calibrated probability.

        TODO(skeleton): isotonic or Platt fit per engine, trained on realized
        outcomes. Matters most in the 0.85-0.98 band this system targets: a
        model that says 0.97 and is right 0.93 of the time turns a positive
        edge negative, and the error is invisible in a win-rate summary.

        Identity until a fit exists, so an uncalibrated engine is obvious
        rather than quietly wrong.
        """
        return raw
