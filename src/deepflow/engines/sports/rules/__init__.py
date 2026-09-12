"""Sport rule registry: league abbreviation -> how to read that sport's feed.

The feed labels every event with a ``league_abbreviation`` (``lal``, ``cfb``,
``wta``, ``lol``), not a sport. Resolving league to sport is therefore the first
thing that has to happen, and getting it wrong means parsing one sport's fields
with another's rules -- which produces a confident wrong number rather than an
error. See :mod:`deepflow.engines.sports.rules.base` for the specific collisions.

Resolution happens in three tiers:

1. **The venue's own league list.** ``get_sports()`` returns 465 leagues, each with
   tag ids, and the tag ids for soccer (172 leagues), cricket (92) and esports (16)
   are reliable. :meth:`SportRegistry.load_from_venue` uses them, so a league the
   venue adds is resolved without a release.
2. **Explicit league sets** below, for the sports whose tag ids are not reliable.
   Measured: tag ``678`` appears on both baseball and basketball leagues, so a
   tag-only mapping would route one to the other.
3. **Payload shape**, which turns out to carry more than expected. The composite
   ``a-b|c-d|BoN`` score is unique to esports, and *period vocabularies are
   sport-specific*: only soccer uses ``1H``/``2H``/``HT``, only tennis uses
   ``S1``/``TB1``, only gridiron uses ``Q1``. This tier is not a nicety -- the venue
   list resolves soccer only over the network, so without it a registry that has not
   called ``get_sports()`` cannot identify the sport the system most wants to trade.

A league that survives all three is ``UNKNOWN`` and is not modelled. 183 of the
venue's 465 leagues currently fall outside the reliable tags -- mostly hockey,
lacrosse and minor codes -- and guessing at those is how a lacrosse game gets priced
with a soccer model.
"""

from __future__ import annotations

import re
from typing import Any, Final

from deepflow.core.logging import get_logger
from deepflow.engines.sports.rules.base import MatchState, SportKind, SportRules
from deepflow.engines.sports.rules.esports import EsportsRules
from deepflow.engines.sports.rules.gridiron import GridironRules
from deepflow.engines.sports.rules.soccer import SoccerRules
from deepflow.engines.sports.rules.tennis import TennisRules

log = get_logger(__name__)

#: Venue tag ids that reliably identify a sport. Only these three are trustworthy:
#: measured against the venue's league list, other candidates collide across sports.
RELIABLE_SPORT_TAGS: Final[dict[str, SportKind]] = {
    "100350": SportKind.SOCCER,
    "517": SportKind.CRICKET,
    "64": SportKind.ESPORTS,
}

#: Leagues named explicitly, for sports whose tags are not reliable. Lowercased
#: comparison; the feed is inconsistent about case (``InProgress`` vs
#: ``inprogress``, and league codes vary too).
EXPLICIT_LEAGUES: Final[dict[SportKind, frozenset[str]]] = {
    SportKind.AMERICAN_FOOTBALL: frozenset({"nfl", "cfb", "ncaaf", "ufl"}),
    SportKind.TENNIS: frozenset(
        {"atp", "wta", "atp-doubles", "wta-doubles", "grand slam", "challenger", "wta challenger"}
    ),
    SportKind.BASKETBALL: frozenset({"nba", "ncaab", "cbb", "euroleague", "nbasl"}),
    SportKind.BASEBALL: frozenset({"mlb", "npb", "kbo"}),
    SportKind.HOCKEY: frozenset({"nhl", "liiga", "hockeychl", "hbehfcl"}),
    SportKind.ESPORTS: frozenset(
        {"cs2", "lol", "dota2", "val", "mlbb", "r6siege", "codmw", "hok", "ea"}
    ),
}

#: Esports composite score, which no other sport produces.
_COMPOSITE_SCORE = re.compile(r"^\s*\d+\s*-\s*\d+\s*\|")

#: Period vocabularies are sport-specific and turn out to be the most reliable
#: offline signal. Only soccer uses halves, only gridiron/basketball use ``Q``, only
#: tennis uses ``S``/``TB``. This matters because the venue's league list resolves
#: soccer only over the network: without this tier a registry that has not called
#: ``get_sports()`` cannot identify the sport the system most wants to trade.
_PERIOD_SIGNATURES: Final[tuple[tuple[re.Pattern[str], SportKind], ...]] = (
    (re.compile(r"^(\d*H|HT|FT( .*)?)$", re.I), SportKind.SOCCER),
    (re.compile(r"^(S|TB)\d+$", re.I), SportKind.TENNIS),
    (re.compile(r"^Q\d+$", re.I), SportKind.AMERICAN_FOOTBALL),
    (re.compile(r"^End \d+$", re.I), SportKind.BASEBALL),
)

#: Sports with a rule set. Everything else parses to nothing and is not modelled.
RULES: Final[dict[SportKind, SportRules]] = {
    SportKind.SOCCER: SoccerRules(),
    SportKind.AMERICAN_FOOTBALL: GridironRules(),
    SportKind.TENNIS: TennisRules(),
    SportKind.ESPORTS: EsportsRules(),
}

#: Sports whose feed carries enough state to form a probability. Verified by parsing
#: a live capture of 22 leagues rather than assumed.
#:
#: * **Soccer** -- score, half and minute. Stoppage is assumed, which widens the band.
#: * **American football** -- score, quarter and a countdown clock.
#: * **Esports** -- maps won and the stated series length, which is a cleaner
#:   discrete problem than either: the target is known rather than assumed.
#:
#: Absent, with reasons that differ and matter:
#:
#: * **Tennis** parses fine and is still unmodellable -- the set score is not sent,
#:   so a games count cannot be placed in the match (see :mod:`.tennis`).
#: * **Cricket** markets exist and are tradeable, but cricket never appears on the
#:   in-play feed at all, so there is no state to read.
MODELLABLE_SPORTS: Final = frozenset(
    {SportKind.SOCCER, SportKind.AMERICAN_FOOTBALL, SportKind.ESPORTS}
)


class SportRegistry:
    """Resolves a feed event to its sport and parses it with that sport's rules."""

    def __init__(self) -> None:
        self._by_league: dict[str, SportKind] = {}
        for kind, leagues in EXPLICIT_LEAGUES.items():
            for league in leagues:
                self._by_league[league] = kind

    async def load_from_venue(self, public_client: Any) -> int:
        """Extend the league map from ``get_sports()``.

        Only the reliable tag ids are used. A league already named explicitly keeps
        its explicit mapping, because the explicit entries exist precisely where the
        tags are known to be wrong.

        Failure is survivable -- the explicit map still works -- so a venue hiccup
        narrows coverage rather than stopping the pipeline.
        """
        try:
            sports = await public_client.get_sports()
        except Exception:
            log.warning("sport_registry.venue_unavailable", exc_info=True)
            return 0

        added = 0
        for sport in sports:
            league = str(getattr(sport, "sport", "") or "").lower()
            if not league or league in self._by_league:
                continue
            ids = {t.strip() for t in str(getattr(sport, "tags", "") or "").split(",")}
            kind = next((RELIABLE_SPORT_TAGS[i] for i in ids if i in RELIABLE_SPORT_TAGS), None)
            if kind is not None:
                self._by_league[league] = kind
                added += 1

        log.info(
            "sport_registry.loaded",
            leagues_known=len(self._by_league),
            added_from_venue=added,
            total_venue_leagues=len(sports),
        )
        return added

    def sport_for(self, event: object) -> SportKind:
        """Resolve an event's sport, falling back to its payload shape."""
        league = str(getattr(event, "league_abbreviation", "") or "").lower()
        known = self._by_league.get(league)
        if known is not None:
            return known

        score = str(getattr(event, "score", "") or "")
        if _COMPOSITE_SCORE.match(score):
            return SportKind.ESPORTS

        period = str(getattr(event, "period", "") or "").strip()
        for pattern, kind in _PERIOD_SIGNATURES:
            if pattern.match(period):
                return kind

        # An mm:ss clock is a countdown within a period, which soccer does not use.
        elapsed = str(getattr(event, "elapsed", "") or "")
        if ":" in elapsed:
            return SportKind.AMERICAN_FOOTBALL

        return SportKind.UNKNOWN

    def parse(self, event: object) -> MatchState | None:
        """Parse an event with its own sport's rules, or ``None`` if unsupported."""
        kind = self.sport_for(event)
        rules = RULES.get(kind)
        if rules is None:
            return None
        return rules.parse(event)

    def is_modellable(self, event: object) -> bool:
        """Whether this event's sport has both rules and a usable feed."""
        return self.sport_for(event) in MODELLABLE_SPORTS

    @property
    def leagues_known(self) -> int:
        return len(self._by_league)
