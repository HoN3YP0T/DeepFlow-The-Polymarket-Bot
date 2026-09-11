"""Tennis win-probability model. Section 6."""

from __future__ import annotations

from deepflow.core.domain import Market, MarketSnapshot, ProbabilityEstimate
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ClobTokenId
from deepflow.engines.base import BaseProbabilityEngine
from deepflow.engines.sports.state import TennisState


class TennisEngine(BaseProbabilityEngine):
    """Win probability from the point/game/set hierarchy.

    Tennis is the one sport here with a near-exact model: given per-player
    serve-hold probabilities, match win probability follows analytically from
    the current point, game and set state. That makes it the most reliable
    model in the system -- and it makes the serve probabilities the thing that
    must be right.

    Scoreline alone is not enough: who is serving can move the probability by
    more than a break of difference at the same score, so a missing server
    field is an abstention, not a defaultable input.
    """

    name = "tennis"
    categories = frozenset({MarketCategory.TENNIS})

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """TODO(skeleton): recursive point -> game -> set -> match model, with
        tiebreak handling and correct deuce/advantage recursion. Abstain when
        the server is unknown or the format (best-of-3/5) is undetermined."""
        raise NotImplementedError("TennisEngine.estimate")

    def _hold_probabilities(self, state: TennisState) -> tuple[float, float] | None:
        """Per-player serve-hold probabilities from live and prior serve stats."""
        raise NotImplementedError("TennisEngine._hold_probabilities")
