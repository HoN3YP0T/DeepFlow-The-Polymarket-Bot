"""What the Polymarket sports stream actually carries.

This module exists to make a hard limit explicit rather than discovering it at
runtime. ``deepflow.engines.sports.state`` models rich per-sport state -- xG,
shots on target, red cards, serve, wickets, rally streaks. **The venue's sports
feed supplies none of that.** Per ``/market-data/realtime-data#sports-stream``
the entire payload is:

    game_id, sportradar_game_id, slug, league_abbreviation, home_team,
    away_team, status, live, ended, score, period, elapsed, finished_at, turn

``score`` is a single ``"<home>-<away>"`` string, not two fields -- and on
esports it is a composite, ``"000-000|1-1|Bo5"`` (rounds | maps | series
length). ``turn`` is possession and is populated for NFL and CFB only. The feed
also carries an explicit disclaimer that it may be delayed, wrong, or
incomplete.

Two things the documented list omits, observed on the wire on 2026-09-13:

* **The wire is camelCase** (``gameId``, ``leagueAbbreviation``, ``homeTeam``).
  The SDK renames them, so only a raw socket reader needs to know -- but a raw
  reader keying on the snake_case names in this module silently sees no games at
  all.
* **There is a second, richer message shape.** College football sent an
  ``eventState`` block -- a typed per-sport envelope (``type:
  "college-football"``, ``footballState``) alongside ``turnProviderId`` and
  ``updatedAt``. The SDK model is ``extra="ignore"``, so **it discards all
  three**. ``eventState.type`` is an authoritative sport discriminator, better
  than any heuristic in ``engines.sports.rules``; reading it requires bypassing
  the SDK model.

Two consequences the engines must respect:

1. **Every field beyond score/period/elapsed needs an external provider.** A
   football model driven by xG and red cards cannot be fed from here; it either
   sources them elsewhere or degrades to a score-and-clock model. The
   ``GameState`` fields are not wrong, but they are not free either -- each one
   is a data-vendor dependency, and an engine that abstains without them will
   abstain permanently on this feed alone.
2. **Cricket and badminton have no *socket* coverage -- which is not the same as
   no coverage.** The documented league set is NFL, NHL, MLB, NBA, CBB, CFB,
   Soccer, Esports and Tennis, and there is no cricket or badminton status
   vocabulary here. But Gamma's event index carries live cricket state anyway: an
   international fixture was observed live with ``score="74-100"`` and
   ``period="Live"``, and open markets on it. It has no ``game_id``, so it is
   reachable through :meth:`games.GammaGameLinks.in_play` and not through a
   socket join. ``CricketEngine`` was disabled on the strength of the stronger
   claim, which was wrong.

Resolving a league code to its sport is
:class:`deepflow.engines.sports.rules.SportRegistry`, and the per-sport status and
period vocabularies live in that package with the code that reads them. This
module carries the wire model only -- an earlier version also held a ``League``
enum, a per-family ``STATUS_VALUES`` table and a ``PERIOD_MEANINGS`` glossary,
all of which were unreferenced, and the last of which was also wrong (it listed
``1Q``-``4Q``; the feed sends ``Q1``-``Q4``).
"""

from __future__ import annotations

from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict

from deepflow.core.enums import MarketCategory

#: Categories with no observed live-state source anywhere on the venue -- not the
#: socket, and not Gamma's event index either.
#:
#: Cricket was in this set until 2026-09-13 and should not be added back on the
#: strength of the socket alone: the socket has no cricket vocabulary, but a live
#: international fixture was observed through
#: :meth:`deepflow.adapters.polymarket.games.GammaGameLinks.in_play` carrying
#: ``score="74-100"`` and ``period="Live"``. Badminton stays because no such
#: observation exists for it yet -- absence of evidence, and recorded as that
#: rather than as a proven gap.
UNSOURCED_CATEGORIES: Final = frozenset({MarketCategory.BADMINTON})


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
