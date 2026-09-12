"""What the Polymarket sports stream actually carries.

This module exists to make a hard limit explicit rather than discovering it at
runtime. ``deepflow.engines.sports.state`` models rich per-sport state -- xG,
shots on target, red cards, serve, wickets, rally streaks. **The venue's sports
feed supplies none of that.** Per ``/market-data/realtime-data#sports-stream``
the entire payload is:

    game_id, sportradar_game_id, slug, league_abbreviation, home_team,
    away_team, status, live, ended, score, period, elapsed, finished_at, turn

``score`` is a single ``"<home>-<away>"`` string, not two fields. ``turn`` is
possession and is populated for NFL and CFB only. The feed also carries an
explicit disclaimer that it may be delayed, wrong, or incomplete.

Two consequences the engines must respect:

1. **Every field beyond score/period/elapsed needs an external provider.** A
   football model driven by xG and red cards cannot be fed from here; it either
   sources them elsewhere or degrades to a score-and-clock model. The
   ``GameState`` fields are not wrong, but they are not free either -- each one
   is a data-vendor dependency, and an engine that abstains without them will
   abstain permanently on this feed alone.
2. **Cricket and badminton are not covered at all.** The documented league set
   is NFL, NHL, MLB, NBA, CBB, CFB, Soccer, Esports and Tennis. There is no
   cricket or badminton status vocabulary, so ``CricketEngine`` and
   ``BadmintonEngine`` have no venue-native state source.

The right response is a settings-level one, not a silent one: see
``SUPPORTED_LEAGUES`` and ``VENUE_NATIVE_CATEGORIES`` below, which the engine
registry uses to refuse to enable an engine whose inputs cannot be sourced.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict

from deepflow.core.enums import MarketCategory


class League(StrEnum):
    """League abbreviations the sports stream is documented to emit."""

    NFL = "NFL"
    NHL = "NHL"
    MLB = "MLB"
    NBA = "NBA"
    CBB = "CBB"
    CFB = "CFB"
    SOCCER = "Soccer"
    ESPORTS = "Esports"
    TENNIS = "Tennis"


SUPPORTED_LEAGUES: Final = frozenset(League)

#: Status vocabularies are **case-sensitive and vary by sport**. Tennis and
#: esports use lowercase; the North American leagues use PascalCase. Comparing
#: with a single normalised casing is how a live game reads as scheduled.
STATUS_VALUES: Final[dict[League, frozenset[str]]] = {
    League.NFL: frozenset(
        {
            "Scheduled",
            "InProgress",
            "Final",
            "F/OT",
            "Suspended",
            "Postponed",
            "Delayed",
            "Canceled",
            "Forfeit",
            "NotNecessary",
        }
    ),
    League.NHL: frozenset(
        {
            "Scheduled",
            "InProgress",
            "Final",
            "F/OT",
            "F/SO",
            "Suspended",
            "Postponed",
            "Delayed",
            "Canceled",
            "Forfeit",
            "NotNecessary",
        }
    ),
    League.MLB: frozenset(
        {
            "Scheduled",
            "InProgress",
            "Final",
            "Suspended",
            "Delayed",
            "Postponed",
            "Canceled",
            "Forfeit",
            "NotNecessary",
        }
    ),
    League.NBA: frozenset(
        {
            "Scheduled",
            "InProgress",
            "Final",
            "F/OT",
            "Suspended",
            "Postponed",
            "Delayed",
            "Canceled",
            "Forfeit",
            "NotNecessary",
        }
    ),
    League.CBB: frozenset(
        {
            "Scheduled",
            "InProgress",
            "Final",
            "F/OT",
            "Suspended",
            "Postponed",
            "Delayed",
            "Canceled",
            "Forfeit",
            "NotNecessary",
        }
    ),
    League.CFB: frozenset(
        {
            "Scheduled",
            "InProgress",
            "Final",
            "F/OT",
            "Suspended",
            "Postponed",
            "Delayed",
            "Canceled",
            "Forfeit",
        }
    ),
    League.SOCCER: frozenset(
        {
            "Scheduled",
            "InProgress",
            "Break",
            "Suspended",
            "PenaltyShootout",
            "Final",
            "Awarded",
            "Postponed",
            "Canceled",
        }
    ),
    League.ESPORTS: frozenset({"not_started", "running", "finished", "postponed", "canceled"}),
    League.TENNIS: frozenset(
        {"scheduled", "inprogress", "suspended", "finished", "postponed", "cancelled"}
    ),
}

#: ``period`` is free-form text whose meaning depends on the sport. Halves and
#: quarters are self-describing; ``End 1`` is an MLB inning and ``2/3`` is a map
#: number in a best-of-three, neither of which parses as a clock.
PERIOD_MEANINGS: Final[dict[str, str]] = {
    "1H": "first half",
    "2H": "second half",
    "HT": "halftime",
    "1Q": "first quarter",
    "2Q": "second quarter",
    "3Q": "third quarter",
    "4Q": "fourth quarter",
    "FT": "full time in regulation",
    "FT OT": "full time after overtime",
    "FT NR": "full time, no result",
}

#: Categories for which live state is obtainable from the venue's own feed.
#: Anything outside this set requires a third-party provider before its engine
#: can be enabled -- which is a procurement decision, not a code TODO.
VENUE_NATIVE_CATEGORIES: Final = frozenset(
    {MarketCategory.FOOTBALL, MarketCategory.TENNIS, MarketCategory.OTHER_SPORTS}
)

#: Categories whose engines exist in this codebase but have no venue-native
#: state feed. Kept explicit so ``registry`` can refuse them loudly.
UNSOURCED_CATEGORIES: Final = frozenset({MarketCategory.CRICKET, MarketCategory.BADMINTON})


class SportsFeedEvent(BaseModel):
    """One ``sport_result`` payload, exactly as documented -- no more.

    Deliberately a flat mirror of the wire format rather than a normalised
    game model. The translation into a per-sport ``GameState`` is lossy and
    sport-specific, and doing it here would hide how little the feed provides.
    """

    model_config = ConfigDict(frozen=True, extra="allow")

    game_id: int
    league_abbreviation: str
    status: str
    live: bool = False
    ended: bool = False
    score: str = ""
    sportradar_game_id: str | None = None
    slug: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    period: str | None = None
    elapsed: str | None = None
    finished_at: datetime | None = None
    turn: str | None = None
    """Possession. NFL and CFB only; ``None`` everywhere else."""

    def scores(self) -> tuple[int, int] | None:
        """``(home, away)`` parsed from the combined ``score`` string.

        Returns ``None`` rather than zeros when the string is absent or
        unparseable. A missing score must reach the model as "unknown": read as
        0-0 it turns a 2-0 lead into a coin flip and invites the model to trade
        a scoreline that never happened.
        """
        parts = self.score.split("-")
        if len(parts) != 2:
            return None
        try:
            return int(parts[0].strip()), int(parts[1].strip())
        except ValueError:
            return None

    def elapsed_seconds(self) -> int | None:
        """``elapsed`` (``"MM:SS"``) as seconds, or ``None`` if not a clock.

        Sports without a running clock leave this empty or use a non-clock
        format, so callers must treat ``None`` as "no clock available" rather
        than as zero minutes played.
        """
        if not self.elapsed:
            return None
        parts = self.elapsed.split(":")
        try:
            numbers = [int(p) for p in parts]
        except ValueError:
            return None
        if len(numbers) == 2:
            return numbers[0] * 60 + numbers[1]
        if len(numbers) == 3:
            return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
        return None
