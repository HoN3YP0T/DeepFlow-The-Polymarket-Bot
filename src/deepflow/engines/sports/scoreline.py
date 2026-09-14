"""Scoreline distribution for a football match in progress.

The model is deliberately the smallest thing that can be right about the question
asked: given the goals already scored and the time left, what is the chance each
result? Remaining goals are treated as independent Poisson draws over the time that
remains, and the three-way result probability follows by summing the joint
distribution.

**Why Poisson.** Goals are rare, roughly independent events in continuous time, which
is the textbook case, and every refinement beyond it needs data this feed does not
send. Dixon-Coles corrects the low-score cells for the dependence between the two
sides; it also needs fitted per-league parameters, and inventing them would be
exactly the plausible default this codebase keeps deleting.

**What is deliberately not modelled, and why it is not a shortcut.** No team strength,
because `rules/soccer.py` lists shots, xG and possession under `UNAVAILABLE` and
nothing supplies a rating -- so the rates here are league-agnostic. That is a real
limitation and it is worst exactly where it would be easiest to hide: early in a
match, when the prior matters most and the scoreline says least. It matters least in
the late-game window this system actually trades, where a two-goal lead at 85' is
about the remaining count of chances and very little about who is playing. The
uncertainty returned grows with the time left, which is the honest expression of it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

#: Goals per match, split by side, from the long-run rates of top-division football.
#:
#: Home 1.50 and away 1.20 is a ~0.3-goal home advantage on a ~2.7-goal match, which
#: is the standard figure and is stable across leagues to well within the error this
#: model carries anyway.
#:
#: **These are league-agnostic and that is a known weakness, not an oversight.** The
#: venue feed sends no shots, no xG, no possession and no team rating (see
#: ``rules/soccer.py``'s ``UNAVAILABLE``), so there is nothing to scale them by. A
#: fitted per-team attack/defence pair is the first upgrade worth making and it needs
#: a data source this system does not have.
HOME_GOALS_PER_MATCH: Final = 1.50
AWAY_GOALS_PER_MATCH: Final = 1.20

#: Regulation match length in seconds, which is what the rates above are *per*.
MATCH_SECONDS: Final = 90 * 60

#: Where to truncate the per-side goal sum.
#:
#: P(6+ more goals from one side in what remains) is below 1e-6 for any rate this
#: model produces, so the truncation is far below the noise in the rates themselves.
#: Ten rather than six purely so the tail is not visible in the arithmetic at all.
MAX_GOALS: Final = 10


@dataclass(frozen=True)
class ResultProbabilities:
    """Three-way result probability. Sums to 1 by construction."""

    home: Decimal
    draw: Decimal
    away: Decimal

    def for_result(self, result: str) -> Decimal | None:
        return {"HOME": self.home, "DRAW": self.draw, "AWAY": self.away}.get(result)


def _poisson_pmf(rate: float, count: int) -> float:
    """P(exactly ``count`` events) for a Poisson with mean ``rate``.

    A rate of zero is not a degenerate case to guard against -- it is the correct
    description of a match with no time left, where the current score is the final
    one with certainty.
    """
    if rate <= 0:
        return 1.0 if count == 0 else 0.0
    return math.exp(-rate) * rate**count / math.factorial(count)


def expected_remaining_goals(seconds_remaining: float) -> tuple[float, float]:
    """Expected goals for (home, away) over the time that remains.

    Linear in time, which is the Poisson assumption restated: a constant hazard. Real
    matches score slightly more in the second half than the first, and the effect is
    small enough that including it without data to fit it would add a parameter and
    no accuracy.
    """
    if seconds_remaining <= 0:
        return 0.0, 0.0
    share = seconds_remaining / MATCH_SECONDS
    return HOME_GOALS_PER_MATCH * share, AWAY_GOALS_PER_MATCH * share


def result_probabilities(
    *,
    home_score: int,
    away_score: int,
    seconds_remaining: float,
) -> ResultProbabilities:
    """P(home win), P(draw), P(away win) from the score and the time left.

    With no time left this returns a certainty on the current scoreline, which is the
    correct answer and also the boundary worth testing: a model that smears
    probability across a finished match is one that will happily buy a 0.99 that is
    really a 1.00 -- or a 0.00.
    """
    home_rate, away_rate = expected_remaining_goals(seconds_remaining)
    home_pmf = [_poisson_pmf(home_rate, goals) for goals in range(MAX_GOALS + 1)]
    away_pmf = [_poisson_pmf(away_rate, goals) for goals in range(MAX_GOALS + 1)]

    lead = home_score - away_score
    home_wins = draws = away_wins = 0.0
    for extra_home, p_home in enumerate(home_pmf):
        for extra_away, p_away in enumerate(away_pmf):
            joint = p_home * p_away
            final = lead + extra_home - extra_away
            if final > 0:
                home_wins += joint
            elif final == 0:
                draws += joint
            else:
                away_wins += joint

    # Renormalise over the truncated grid rather than letting the lost tail leak away
    # as missing probability. The residual is ~1e-7, but a distribution that does not
    # sum to one is a bug waiting to be read as an edge.
    total = home_wins + draws + away_wins
    if total <= 0:
        raise ValueError("scoreline distribution summed to zero")

    return ResultProbabilities(
        home=_to_decimal(home_wins / total),
        draw=_to_decimal(draws / total),
        away=_to_decimal(away_wins / total),
    )


def _to_decimal(value: float) -> Decimal:
    """Eight places, which is the precision the probability columns carry."""
    return Decimal(str(round(value, 8)))
