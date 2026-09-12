"""Esports rules.

Feed shape, from live capture: ``score='2-0|0-1|Bo5'``, ``period='2/5'``.

Three fields packed into one string, confirmed by watching a ``lol`` series move
from ``'0-0|0-0|Bo5'`` at period ``1/5`` to ``'2-0|0-1|Bo5'`` at ``2/5``:

1. rounds in the **current map** (``2-0``), sometimes zero-padded (``000-000``)
2. **maps won** in the series (``0-1``)
3. series length (``Bo5``)

Maps won is the score that decides the match; rounds decide the current map. Reading
the first field as the series score is the obvious mistake and it inverts the
meaning -- ``'2-0|0-1|Bo5'`` is a player *behind* in the series while ahead on the
current map.

Series structure is the one thing this feed gives that soccer's does not: ``Bo5``
states the format, so the target is known rather than assumed.
"""

from __future__ import annotations

import math
import re
from typing import Final

from deepflow.engines.sports.rules.base import MatchState, SportKind, parse_pair

_COMPOSITE = re.compile(
    r"^\s*(?P<rounds>[\d]+\s*-\s*[\d]+)\s*\|\s*(?P<maps>\d+\s*-\s*\d+)\s*\|\s*Bo(?P<best_of>\d+)\s*$",
    re.I,
)

LIVE_STATUSES: Final = frozenset({"running", "inprogress", "in_progress"})
ENDED_STATUSES: Final = frozenset({"finished", "canceled", "cancelled", "postponed"})

UNAVAILABLE: Final = ("economy", "player_form", "map_pool", "side_selection")


class EsportsRules:
    """Reads esports feed events."""

    kind = SportKind.ESPORTS
    regulation_seconds: int | None = None
    periods: int | None = None

    def parse(self, event: object) -> MatchState | None:
        raw = str(getattr(event, "score", "") or "")
        match = _COMPOSITE.match(raw)
        if match is None:
            return None

        maps = parse_pair(match.group("maps"))
        rounds = parse_pair(match.group("rounds"))
        best_of = int(match.group("best_of"))
        status = str(getattr(event, "status", "") or "").lower()

        return MatchState(
            sport=SportKind.ESPORTS,
            league=str(getattr(event, "league_abbreviation", "") or ""),
            home_team=getattr(event, "home_team", None),
            away_team=getattr(event, "away_team", None),
            # Maps won, not rounds. Rounds decide the current map; maps decide the
            # match, and only the match is what a moneyline market pays on.
            home_score=maps[0] if maps else None,
            away_score=maps[1] if maps else None,
            period_index=(maps[0] + maps[1] + 1) if maps else None,
            periods_total=best_of,
            period_label=str(getattr(event, "period", "") or "") or None,
            is_live=bool(getattr(event, "live", False)) or status in LIVE_STATUSES,
            ended=bool(getattr(event, "ended", False)) or status in ENDED_STATUSES,
            unavailable=(*UNAVAILABLE, f"current_map_rounds={rounds}" if rounds else "rounds"),
        )

    @staticmethod
    def maps_to_win(best_of: int) -> int:
        """Maps needed to take the series."""
        return math.ceil(best_of / 2)
