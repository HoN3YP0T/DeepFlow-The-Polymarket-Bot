"""Probability engine port."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from deepflow.core.domain import (
    Classification,
    Market,
    MarketSnapshot,
    ProbabilityEstimate,
)
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ClobTokenId


@runtime_checkable
class ProbabilityEnginePort(Protocol):
    """Estimates an outcome probability independently of the market price.

    The independence matters: an engine that reads the market price and nudges
    it produces an edge that is an artefact, not a signal. Implementations take
    the snapshot for microstructure and timing, but derive their probability
    from state (score, clock, strike distance, event evidence).
    """

    name: str
    categories: frozenset[MarketCategory]

    def supports(self, classification: Classification) -> bool: ...

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """Return ``None`` when the engine cannot form a view.

        Returning ``None`` is the correct, safe answer for missing game state.
        A fabricated 0.5 would flow into EV as a real opinion.
        """
        ...
