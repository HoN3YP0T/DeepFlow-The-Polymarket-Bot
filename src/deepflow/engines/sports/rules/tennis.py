"""Tennis rules, and the reason tennis cannot be modelled from this feed.

Feed shape, from live capture: ``score='2-3'``, ``period='S1'``, no clock.

The decisive observation is what ``score`` counts. A ``wta`` match was seen going
``'2-1'`` then ``'2-3'`` while ``period`` stayed ``'S1'``, and a ``grand slam``
match moved ``S1 -> S2`` with the score resetting to ``'0-0'``. So **score is games
within the current set**, and the number of sets each player has already won is
never transmitted.

That is not a gap that can be worked around. A player 2-3 down in games during set
three could be two sets up or two sets down -- the same feed payload describes a
match that is nearly won and one that is nearly lost. Point score, serve and
tiebreak state are absent too, and serve dominates short-horizon tennis probability.

So :class:`TennisRules` parses the feed faithfully and
:attr:`MatchState.is_modellable` is forced to ``False``: the state is readable and
insufficient. The engine abstains structurally rather than producing a number from
games alone.
"""

from __future__ import annotations

from typing import Final

from deepflow.engines.sports.rules.base import (
    MatchState,
    SportKind,
    parse_pair,
    period_number,
)

LIVE_STATUSES: Final = frozenset({"inprogress", "in_progress", "running"})
ENDED_STATUSES: Final = frozenset({"finished", "cancelled", "canceled", "postponed"})

#: Without the set score, a games count cannot be placed in the match at all: the
#: same payload describes a match nearly won and one nearly lost. No assumption
#: substitutes for it, so it disqualifies rather than degrades.
BLOCKING: Final = ("sets_won_by_each_player",)

#: The rest would improve a tennis model that could exist. Serve matters most --
#: it dominates short-horizon tennis probability -- but it is moot while the set
#: score is missing.
UNAVAILABLE: Final = (
    "point_score",
    "server",
    "tiebreak_state",
    "break_points",
    "service_hold_rates",
)


class TennisRules:
    """Reads tennis feed events. Parsing succeeds; modelling does not."""

    kind = SportKind.TENNIS
    regulation_seconds: int | None = None
    periods: int | None = None
    """Best-of-3 or best-of-5 is not in the feed either, so the match length is
    unknown as well as the set score."""

    def parse(self, event: object) -> MatchState | None:
        status = str(getattr(event, "status", "") or "").lower()
        games = parse_pair(getattr(event, "score", None))
        label = getattr(event, "period", None)

        return MatchState(
            sport=SportKind.TENNIS,
            league=str(getattr(event, "league_abbreviation", "") or ""),
            home_team=getattr(event, "home_team", None),
            away_team=getattr(event, "away_team", None),
            # Games in the current set, NOT sets won. Named through the field it
            # shares with other sports, so the unavailable list is what stops a
            # model reading it as a match score.
            home_score=games[0] if games else None,
            away_score=games[1] if games else None,
            period_index=period_number(label, ("S", "TB")),
            periods_total=None,
            period_label=str(label) if label else None,
            is_live=bool(getattr(event, "live", False)) or status in LIVE_STATUSES,
            ended=bool(getattr(event, "ended", False)) or status in ENDED_STATUSES,
            unavailable=UNAVAILABLE,
            blocking_gaps=BLOCKING,
        )
