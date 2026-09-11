"""Political event model. Section 13.

Elections, appointments, court and policy decisions. Unlike sports, there is no
continuously observable state that moves the true probability -- it updates in
discrete jumps on announcements, and sits still in between.

The consequence for this system: most of the time the honest estimate is
"the market is probably right", and the engine abstains. It has an opinion only
when it holds evidence the market has not priced, which in practice means a
verified event from the geopolitical event pipeline. An engine that always
produces a number here would be manufacturing edge out of nothing.
"""

from __future__ import annotations

from deepflow.core.domain import Market, MarketSnapshot, ProbabilityEstimate
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ClobTokenId
from deepflow.engines.base import BaseProbabilityEngine


class PoliticalEngine(BaseProbabilityEngine):
    """Probability for political and policy markets."""

    name = "political"
    categories = frozenset({MarketCategory.POLITICS})

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """TODO(skeleton): start from a base rate (polling, prior, scheduled
        timetable), then apply verified events from the event pipeline. Carry
        wide uncertainty and abstain whenever no unpriced evidence is held."""
        raise NotImplementedError("PoliticalEngine.estimate")
