"""Football win probability from the score and the clock.

Roadmap item 12, and the last piece of Phase 3.

**What this model is.** Remaining goals as independent Poisson draws over the time
that remains, summed into a three-way result distribution -- see
:mod:`deepflow.engines.sports.scoreline`. That is the whole model, and it is the
largest one the venue's feed can support: score, period and clock are what it sends.

**What this model is not, stated plainly because the previous version of this file
promised it.** No red cards, no xG, no shots, no possession, no team strength. Those
appear in `rules/soccer.py`'s own ``UNAVAILABLE`` list, and the docstring that used to
sit here described a model driven by them -- "red cards are a structural change, not a
modifier" -- for inputs that have never existed in this system. A fitted attack/defence
pair per team is the first upgrade worth making and it needs a data source we do not
have; until then the goal rates are league-agnostic and the uncertainty says so.

**The join it depends on.** An engine receives a market, a book and a token id, none
of which carry a score, so the live state is kept in a
:class:`~deepflow.engines.sports.live_state.MatchStateStore` that the orchestrator's
fixture sweep fills -- the same shape as the crypto model's TWAP reference.

**Which market is which.** A soccer result market is a *three-way group* of separate
binary markets -- "Will Coquimbo win?", "Will it end in a draw?", "Will Huachipato
win?" -- and computing P(home win) is useless without knowing which of the three is in
front of you. That comes from ``group_item_title`` matched against the fixture's own
team names, never from outcome position.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from deepflow.config.thresholds import StrategyThresholds
from deepflow.core.domain import Market, MarketSnapshot, ProbabilityEstimate
from deepflow.core.enums import MarketCategory
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId
from deepflow.engines.base import BaseProbabilityEngine
from deepflow.engines.sports.live_state import FixtureObservation, MatchStateStore
from deepflow.engines.sports.rules.base import MatchState, SportKind
from deepflow.engines.sports.rules.soccer import SoccerRules
from deepflow.engines.sports.scoreline import MATCH_SECONDS, result_probabilities

log = get_logger(__name__)

#: The only sports market type this engine answers for.
#:
#: ``moneyline`` is the full-time result. The same fixture also lists
#: ``soccer_halftime_result`` and ``soccer_second_half_result`` families, which ask a
#: different question -- who leads *at half time* is not who wins -- and answering them
#: with a full-time distribution would be confidently wrong rather than approximate.
FULL_TIME_RESULT: Final = "moneyline"

#: How far a league-average pair of goal rates can be from the right pair, expressed
#: as probability at kickoff.
#:
#: This model has no team ratings, so a title favourite against a relegation side is
#: priced identically to two mid-table teams. Pre-match probabilities across a typical
#: division span roughly 0.25 either side of the league-average match, and that is the
#: error being carried. It is scaled by the share of the match still to play: with five
#: minutes left the teams' relative strength barely matters, because what decides the
#: result is the scoreline already on the board.
STRENGTH_UNCERTAINTY: Final = Decimal("0.25")

#: Floor on the reported uncertainty.
#:
#: Even at the final whistle this model is a Poisson approximation applied to a feed
#: whose clock is a minute-resolution integer. Claiming less than two points of
#: uncertainty anywhere would invite the Kelly sizing to treat a late-game number as
#: near-certain, which is the failure this floor exists to prevent.
MIN_UNCERTAINTY: Final = Decimal("0.02")

_DRAW_PREFIX: Final = "draw"


def _normalise(text: str | None) -> str:
    return " ".join((text or "").split()).casefold()


class FootballEngine(BaseProbabilityEngine):
    """Three-way result probability from score, clock and assumed stoppage."""

    name = "football"
    categories = frozenset({MarketCategory.FOOTBALL})

    def __init__(self, thresholds: StrategyThresholds, *, states: MatchStateStore) -> None:
        self._thresholds = thresholds
        self._states = states
        self._rules = SoccerRules()

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """P(this market's result) for the fixture as it currently stands.

        Abstains -- returning ``None`` rather than a defaulted number -- when:

        * the market is not a full-time result market;
        * no live state is held for it, or the state held has gone stale (a current
          book beside an old score is the most dangerous combination there is);
        * the fixture is not actually in play, has ended, or its state is missing a
          score or a clock;
        * the market's entity cannot be matched to home, draw or away.

        The last one is the interesting abstention. Knowing P(home win) is worthless
        without knowing whether this market pays on it, and there is no safe fallback:
        guessing wrong prices a trade at exactly its complement.
        """
        if market.sports_market_type != FULL_TIME_RESULT:
            return None

        observation = self._states.get(market.condition_id)
        if observation is None:
            return None

        state = observation.state
        if state.sport is not SportKind.SOCCER:
            return None
        if not state.is_modellable:
            log.info(
                "football.state_not_modellable",
                condition_id=str(market.condition_id),
                blocking=list(state.blocking_gaps),
                live=state.is_live,
                ended=state.ended,
            )
            return None

        remaining = self._seconds_remaining(state)
        if remaining is None:
            return None

        result = self._result_for(market, observation)
        if result is None:
            return None

        assert state.home_score is not None and state.away_score is not None
        probabilities = result_probabilities(
            home_score=state.home_score,
            away_score=state.away_score,
            seconds_remaining=remaining,
        )
        raw = probabilities.for_result(result)
        if raw is None:
            return None

        uncertainty = self._uncertainty(
            state=state,
            remaining=remaining,
            result=result,
            probability=raw,
        )
        return ProbabilityEstimate(
            token_id=token_id,
            model_probability=raw,
            calibrated_probability=self._calibrate(raw),
            uncertainty=uncertainty,
            engine=self.name,
            inputs={
                "result": result,
                "score": f"{state.home_score}-{state.away_score}",
                "half": str(state.period_index),
                "seconds_remaining": str(int(remaining)),
                "home_team": observation.home_team,
                "away_team": observation.away_team,
                "p_home": str(probabilities.home),
                "p_draw": str(probabilities.draw),
                "p_away": str(probabilities.away),
                # Recorded so a journal row says whether the number was adjusted by a
                # fitted curve or merely passed through one that had no evidence there.
                "calibration_supported": str(self._calibration_is_supported(raw)),
            },
        )

    def _seconds_remaining(self, state: MatchState) -> float | None:
        """Regulation time left plus the stoppage the feed never sends.

        ``None`` when the clock is unreadable, which is not the same as zero: the feed
        sends an empty ``elapsed`` at half time and full time, and reading that as "no
        time left" would price a match at the interval as though it were over.

        Stoppage is added from ``SoccerRules.expected_stoppage_seconds`` -- assumed,
        not observed, and deliberately generous. At 87' an extra five minutes is a
        ~40% increase in the chances remaining, so assuming two when it is seven
        understates an equaliser by more than a third. Because it is assumed, its
        effect on the answer is measured and charged to the uncertainty below.
        """
        if state.seconds_remaining is None:
            return None
        return float(
            state.seconds_remaining + self._rules.expected_stoppage_seconds(state.period_index)
        )

    def _result_for(self, market: Market, observation: FixtureObservation) -> str | None:
        """Which of HOME / DRAW / AWAY this market pays on, or ``None``.

        Matched on ``group_item_title`` against the fixture's own team names. That
        field is the venue's statement of *which entity this market is about* -- "CD
        Coquimbo Unido", "Draw (CD Coquimbo Unido vs. CD Huachipato)" -- and it is the
        only thing distinguishing the three markets of a result group, whose outcome
        labels are all just Yes/No.

        **Draw is checked first, and that ordering is load-bearing.** The draw
        market's title contains *both* team names, so a substring test against the
        home name matches it and the model prices a draw as a home win. Team matching
        is exact for the same reason.

        ``None`` on anything unrecognised. There is no defensible fallback: the three
        results are mutually exclusive and picking wrong prices the exact complement
        of the trade being made.
        """
        title = _normalise(market.group_item_title)
        if not title:
            return None
        if title.startswith(_DRAW_PREFIX):
            return "DRAW"
        if title == _normalise(observation.home_team):
            return "HOME"
        if title == _normalise(observation.away_team):
            return "AWAY"

        log.info(
            "football.entity_unmatched",
            condition_id=str(market.condition_id),
            group_item_title=market.group_item_title,
            home_team=observation.home_team,
            away_team=observation.away_team,
        )
        return None

    def _uncertainty(
        self,
        *,
        state: MatchState,
        remaining: float,
        result: str,
        probability: Decimal,
    ) -> Decimal:
        """How wrong this estimate could reasonably be.

        Two terms, and both are computed rather than asserted.

        The first is the **stoppage sensitivity**, measured by re-pricing the match
        without the assumed stoppage and taking the difference. The feed does not send
        stoppage time, so the assumption is doing real work, and this is precisely how
        much: a few points late in a tight game, near zero when the result is already
        settled.

        The second is the **missing team strength**, scaled by the share of the match
        still to play. At kickoff it is the dominant error, because the scoreline says
        nothing and the teams say everything; by the 85th minute it is nearly gone, as
        the goals already scored have decided most of the question.

        Added rather than combined in quadrature: they are not independent errors to
        average away, they are two things this model does not know, and the sizing on
        the other side of this number should see the sum.
        """
        without_stoppage = max(
            remaining - self._rules.expected_stoppage_seconds(state.period_index), 0.0
        )
        assert state.home_score is not None and state.away_score is not None
        alternative = result_probabilities(
            home_score=state.home_score,
            away_score=state.away_score,
            seconds_remaining=without_stoppage,
        ).for_result(result)
        stoppage_sensitivity = (
            abs(probability - alternative) if alternative is not None else Decimal(0)
        )

        share = Decimal(str(min(remaining / MATCH_SECONDS, 1.0)))
        strength = STRENGTH_UNCERTAINTY * share

        total = stoppage_sensitivity + strength
        return min(Decimal(1), max(MIN_UNCERTAINTY, total))
