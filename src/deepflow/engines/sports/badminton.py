"""Badminton win-probability model. Section 6."""

from __future__ import annotations

from deepflow.core.domain import Market, MarketSnapshot, ProbabilityEstimate
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ClobTokenId
from deepflow.engines.base import BaseProbabilityEngine
from deepflow.engines.sports.state import BadmintonState


class BadmintonEngine(BaseProbabilityEngine):
    """Win probability from rally-point game state.

    Rally scoring makes every rally a point, so the game is short and the
    variance per point is high. Leads late in a game to 21 are strong but not
    decisive -- the two-point margin and 30-point cap mean a 20-18 game point
    is materially less safe than the raw differential suggests, which is
    precisely the kind of market that trades in the 0.85-0.98 band.
    """

    name = "badminton"
    categories = frozenset({MarketCategory.BADMINTON})

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """TODO(skeleton): per-rally win probability -> game -> match, with the
        two-point margin rule and the 30-point cap modelled explicitly. Abstain
        on missing point state or an unknown match format."""
        raise NotImplementedError("BadmintonEngine.estimate")

    def _rally_win_probability(self, state: BadmintonState) -> float | None:
        raise NotImplementedError("BadmintonEngine._rally_win_probability")
