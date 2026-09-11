"""Cricket win-probability model. Section 6."""

from __future__ import annotations

from deepflow.core.domain import Market, MarketSnapshot, ProbabilityEstimate
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ClobTokenId
from deepflow.engines.base import BaseProbabilityEngine
from deepflow.engines.sports.state import CricketState


class CricketEngine(BaseProbabilityEngine):
    """Win probability from the chase state.

    The controlling variables in a run chase are runs required, balls
    remaining, and wickets in hand -- jointly, not separately. A required rate
    of 12 with nine wickets standing and the same rate with two wickets
    standing are different games, and a model that reads only the rate will
    price them identically.

    Format changes the model, not just its constants: a Test draw is a real
    outcome with no T20 analogue, so ``match_format`` is required input and an
    unknown format is an abstention.
    """

    name = "cricket"
    categories = frozenset({MarketCategory.CRICKET})

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """TODO(skeleton): resource-based chase model (runs required, balls
        remaining, wickets in hand) conditioned on format, adjusted for batting
        and bowling strength and the current partnership. Abstain on unknown
        format, missing innings state, or a stale feed."""
        raise NotImplementedError("CricketEngine.estimate")

    def _chase_probability(self, state: CricketState) -> float | None:
        raise NotImplementedError("CricketEngine._chase_probability")
