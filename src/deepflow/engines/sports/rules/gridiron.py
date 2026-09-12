"""American football rules.

Feed shape, from live capture (``cfb``): ``score='0-10'`` points, ``period='Q1'``,
``elapsed='05:04'``.

The clock is the trap. ``elapsed`` here is **mm:ss remaining in the current
quarter**, counting *down* -- not time played. A ``cfb`` game at ``Q1`` with
``'05:04'`` has about 40 minutes left, while the same string read as soccer-style
minutes elapsed would say five minutes played and 85 remaining. Two sports, one
field, opposite meanings.

Possession (``turn``) is documented as populated for NFL and CFB only, and is the
one piece of state this feed gives that the others do not.
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

QUARTER_SECONDS: Final = 15 * 60
PERIODS: Final = 4
REGULATION_SECONDS: Final = QUARTER_SECONDS * PERIODS

LIVE_STATUSES: Final = frozenset({"inprogress", "in_progress"})
ENDED_STATUSES: Final = frozenset(
    {"final", "f/ot", "canceled", "cancelled", "postponed", "forfeit", "notnecessary"}
)

UNAVAILABLE: Final = ("down", "distance", "field_position", "timeouts", "drive_state")


class GridironRules:
    """Reads NFL and college-football feed events."""

    kind = SportKind.AMERICAN_FOOTBALL
    regulation_seconds: int | None = REGULATION_SECONDS
    periods: int | None = PERIODS

    def parse(self, event: object) -> MatchState | None:
        status = str(getattr(event, "status", "") or "").lower()
        points = parse_pair(getattr(event, "score", None))
        label = getattr(event, "period", None)
        quarter = period_number(label, ("Q",))
        in_quarter_remaining = parse_clock(getattr(event, "elapsed", None))

        remaining: int | None = None
        elapsed: int | None = None
        if quarter is not None and in_quarter_remaining is not None:
            quarters_left_after_this = max(PERIODS - quarter, 0)
            remaining = in_quarter_remaining + quarters_left_after_this * QUARTER_SECONDS
            elapsed = max(REGULATION_SECONDS - remaining, 0)

        return MatchState(
            sport=SportKind.AMERICAN_FOOTBALL,
            league=str(getattr(event, "league_abbreviation", "") or ""),
            home_team=getattr(event, "home_team", None),
            away_team=getattr(event, "away_team", None),
            home_score=points[0] if points else None,
            away_score=points[1] if points else None,
            period_index=quarter,
            periods_total=PERIODS,
            period_label=str(label) if label else None,
            seconds_elapsed=elapsed,
            seconds_remaining=remaining,
            is_live=bool(getattr(event, "live", False)) or status in LIVE_STATUSES,
            ended=bool(getattr(event, "ended", False)) or status in ENDED_STATUSES,
            unavailable=UNAVAILABLE,
        )
