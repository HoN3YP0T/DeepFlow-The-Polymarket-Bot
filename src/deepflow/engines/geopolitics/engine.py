"""Geopolitical probability engine. Sections 13-14."""

from __future__ import annotations

from deepflow.config.thresholds import GeopoliticsThresholds
from deepflow.core.domain import Market, MarketSnapshot, ProbabilityEstimate
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ClobTokenId
from deepflow.engines.base import BaseProbabilityEngine
from deepflow.engines.geopolitics.events import EventPipeline


class GeopoliticalEngine(BaseProbabilityEngine):
    """Probability for conflict, ceasefire and diplomatic markets.

    Conflict categories are classified dynamically. The brief lists current
    theatres as examples; hardcoding them would leave the system blind to the
    next one, which is precisely when these markets are most mispriced.
    """

    name = "geopolitical"
    categories = frozenset(
        {
            MarketCategory.GEOPOLITICS,
            MarketCategory.WAR_CONFLICT,
            MarketCategory.CEASEFIRE,
            MarketCategory.MILITARY_DIPLOMATIC,
        }
    )

    def __init__(self, pipeline: EventPipeline, thresholds: GeopoliticsThresholds) -> None:
        self._pipeline = pipeline
        self._thresholds = thresholds

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """TODO(skeleton): base rate from the market's own history and time to
        deadline, updated by actionable events mapped to this market. Abstain
        when no actionable event is held -- with no unpriced evidence, the
        market price is the better estimate and there is no edge to take."""
        raise NotImplementedError("GeopoliticalEngine.estimate")
