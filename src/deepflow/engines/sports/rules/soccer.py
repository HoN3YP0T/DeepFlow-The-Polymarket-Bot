"""Soccer rules.

Feed shape, from live capture: ``score='2-0'`` goals, ``period='1H'|'2H'|'HT'|'FT'``,
``elapsed='23'`` minutes **counting up**. At ``HT`` and ``FT`` the feed sends
``elapsed=''`` rather than a number, which is why an empty clock must read as
"unknown", not zero.
"""

from __future__ import annotations

from typing import Final

from deepflow.engines.sports.rules.base import (
    MatchState,
    SportKind,
    parse_clock,
    parse_pair,
    period_number,
)

REGULATION_SECONDS: Final = 90 * 60
HALF_SECONDS: Final = 45 * 60
PERIODS: Final = 2

#: The feed gives no stoppage-time indicator, so it has to be assumed. These are
#: deliberately conservative modern figures rather than the textbook 1-2 minutes.
#:
#: This matters most exactly where the late-game strategy operates: at 87' an extra
#: 5 minutes is a ~40% increase in remaining scoring opportunities, so a model that
#: assumes 2 minutes and gets 7 has understated the chance of an equaliser by more
#: than a third. Because the figure is assumed rather than observed, it is a primary
#: contributor to the estimate's uncertainty, not a free parameter.
EXPECTED_STOPPAGE_FIRST_HALF_SECONDS: Final = 2 * 60
EXPECTED_STOPPAGE_SECOND_HALF_SECONDS: Final = 5 * 60

#: Feed statuses meaning play is under way, or paused but not finished.
LIVE_STATUSES: Final = frozenset({"inprogress", "in_progress", "running"})
BREAK_STATUSES: Final = frozenset({"break", "halftime", "ht", "suspended", "delayed"})
ENDED_STATUSES: Final = frozenset(
    {"final", "ft", "awarded", "canceled", "cancelled", "postponed", "f/ot", "f/so"}
)

#: What a soccer model needs and the feed does not send.
UNAVAILABLE: Final = (
    "stoppage_time",
    "red_cards",
    "shots",
    "shots_on_target",
    "expected_goals",
    "possession",
    "substitutions",
)


class SoccerRules:
    """Reads soccer feed events."""

    kind = SportKind.SOCCER
    regulation_seconds: int | None = REGULATION_SECONDS
    periods: int | None = PERIODS

    def parse(self, event: object) -> MatchState | None:
        league = str(getattr(event, "league_abbreviation", "") or "")
        status = str(getattr(event, "status", "") or "").lower()
        label = getattr(event, "period", None)
        goals = parse_pair(getattr(event, "score", None))
        clock = parse_clock(getattr(event, "elapsed", None))
        ended = bool(getattr(event, "ended", False)) or status in ENDED_STATUSES

        half = period_number(label, ("H",))
        if half is None and label:
            # HT and FT carry no number. Half time is the end of the first half;
            # full time is the end of the match.
            upper = str(label).strip().upper()
            half = 1 if upper == "HT" else PERIODS if upper.startswith("FT") else None

        seconds_elapsed = clock
        remaining: int | None = None
        if seconds_elapsed is not None:
            # The clock counts up through the whole match, so remaining is simply
            # regulation minus elapsed -- before any stoppage, which is unknown.
            remaining = max(REGULATION_SECONDS - seconds_elapsed, 0)
        elif half == 1 and str(label).strip().upper() == "HT":
            remaining = REGULATION_SECONDS - HALF_SECONDS

        return MatchState(
            sport=SportKind.SOCCER,
            league=league,
            home_team=getattr(event, "home_team", None),
            away_team=getattr(event, "away_team", None),
            home_score=goals[0] if goals else None,
            away_score=goals[1] if goals else None,
            period_index=half,
            periods_total=PERIODS,
            period_label=str(label) if label else None,
            seconds_elapsed=seconds_elapsed,
            seconds_remaining=remaining,
            is_live=bool(getattr(event, "live", False)) or status in LIVE_STATUSES,
            is_break=status in BREAK_STATUSES,
            ended=ended,
            unavailable=UNAVAILABLE,
        )

    @staticmethod
    def expected_stoppage_seconds(half: int | None) -> int:
        """Stoppage to assume for the remainder of the match."""
        if half == 1:
            return EXPECTED_STOPPAGE_FIRST_HALF_SECONDS + EXPECTED_STOPPAGE_SECOND_HALF_SECONDS
        return EXPECTED_STOPPAGE_SECOND_HALF_SECONDS
