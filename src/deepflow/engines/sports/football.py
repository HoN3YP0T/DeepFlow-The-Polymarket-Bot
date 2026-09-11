"""Football win-probability model. Sections 6-8."""

from __future__ import annotations

from deepflow.core.domain import Market, MarketSnapshot, ProbabilityEstimate
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ClobTokenId
from deepflow.engines.base import BaseProbabilityEngine
from deepflow.engines.sports.state import FootballState


class FootballEngine(BaseProbabilityEngine):
    """Win probability from score, clock and game state.

    Core shape: P(lead holds) rises non-linearly as the clock runs out, so the
    model is driven by remaining scoring opportunities rather than by minutes.
    A two-goal lead at 85' is not "10 minutes safer" than at 75' -- it is
    roughly half as likely to be overturned, because what remains is the count
    of realistic chances, and stoppage time is part of that count.

    Red cards are a structural change, not a modifier: they alter the expected
    chance rate for the rest of the match and must shift the distribution, not
    nudge the point estimate.
    """

    name = "football"
    categories = frozenset({MarketCategory.FOOTBALL})

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """Estimate the outcome probability.

        TODO(skeleton):
        * resolve which team ``token_id`` pays out on, from the *resolution
          criteria* rather than the outcome label
        * effective time remaining = 90 - minute + expected stoppage
        * expected remaining goals per side, from a base rate scaled by team
          strength, red cards, and live shot/xG rates where available
        * P(outcome) from the resulting scoreline distribution
        * abstain when score, minute, or a fresh feed is missing

        Late-game window (section 8) is a *gate* applied by the signal engine,
        not a term in this model. Keeping the entry window out of the
        probability keeps the model honest and the policy tunable.
        """
        raise NotImplementedError("FootballEngine.estimate")

    def _expected_remaining_goals(self, state: FootballState) -> tuple[float, float] | None:
        """Expected goals for (home, away) over the remainder. ``None`` if the
        state is too incomplete to say."""
        raise NotImplementedError("FootballEngine._expected_remaining_goals")
