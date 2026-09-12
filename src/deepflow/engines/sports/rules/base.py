"""Per-sport interpretation of the venue's sports feed.

One rule set per sport, never one rule for all. The feed sends the same eight
fields for every game, and those fields mean different things per sport -- measured
from a live capture of 22 leagues:

===================  ==========================  =========  ==============================
sport                score                       period     elapsed
===================  ==========================  =========  ==============================
soccer               ``'2-0'`` goals             ``'1H'``   ``'23'`` minutes, counting up
american football    ``'0-10'`` points           ``'Q1'``   ``'05:04'`` mm:ss, counting DOWN
tennis               ``'2-3'`` games *this set*  ``'S1'``   absent
esports              ``'0-0|0-1|Bo5'``           ``'2/5'``  absent
===================  ==========================  =========  ==============================

Reading any of those with another sport's rules produces a confident wrong answer
rather than an error: ``'05:04'`` parsed as soccer minutes is five minutes played
instead of five remaining, and ``'2-3'`` read as a tennis *set* score is a match
nearly over instead of one barely begun.

Each rule set also declares what its sport's feed does **not** carry, in
:attr:`MatchState.unavailable`. That list is the input to a data-feed purchase
decision, and it is per sport because the gaps are per sport: tennis is missing the
set score and serve, soccer is missing stoppage time, and neither absence tells you
anything about the other.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class SportKind(StrEnum):
    """Sports the feed distinguishes, by how their state must be read."""

    SOCCER = "SOCCER"
    AMERICAN_FOOTBALL = "AMERICAN_FOOTBALL"
    TENNIS = "TENNIS"
    ESPORTS = "ESPORTS"
    CRICKET = "CRICKET"
    BASKETBALL = "BASKETBALL"
    BASEBALL = "BASEBALL"
    HOCKEY = "HOCKEY"
    UNKNOWN = "UNKNOWN"


class MatchState(BaseModel):
    """Normalized live state for one game.

    Deliberately thin. It carries what the feed actually provides plus an explicit
    record of what it does not, so a model reading this cannot mistake an absent
    field for a zero.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    sport: SportKind
    league: str
    home_team: str | None = None
    away_team: str | None = None

    home_score: int | None = None
    away_score: int | None = None
    """Units differ by sport: goals, points, games in the current set, maps won."""

    period_index: int | None = None
    """1-based. Half, quarter, set or map number."""
    periods_total: int | None = None
    """Regulation periods. ``None`` where the sport's format is not in the feed."""
    period_label: str | None = None

    seconds_elapsed: int | None = None
    """Time played in the *match*, where the sport has a clock at all."""
    seconds_remaining: int | None = None
    """Regulation time left, excluding any stoppage the feed does not report."""

    is_live: bool = False
    is_break: bool = False
    ended: bool = False

    unavailable: tuple[str, ...] = ()
    """State this sport needs for modelling that the feed does not supply.

    Degrading, not disqualifying: soccer's missing stoppage time widens the
    uncertainty band but a score-and-clock model still works without it."""

    blocking_gaps: tuple[str, ...] = ()
    """Absent state without which no probability can be formed at all.

    Distinct from :attr:`unavailable` because the consequences differ. Tennis is
    missing the set score, and a games count cannot be placed in a match without it
    -- the identical payload describes a match nearly won and one nearly lost. That
    is disqualifying. Soccer's missing stoppage merely widens the band.

    Kept as a list of names rather than a boolean so the abstention says *what* was
    missing, which is what a journal entry and a data-feed decision both need."""

    @property
    def score_difference(self) -> int | None:
        """Home minus away, or ``None`` if either side is unknown.

        ``None`` rather than 0: a missing score and a level game are opposite
        situations, and the second is tradeable.
        """
        if self.home_score is None or self.away_score is None:
            return None
        return self.home_score - self.away_score

    @property
    def is_modellable(self) -> bool:
        """Whether enough state exists to attempt a probability at all.

        A blocking gap disqualifies regardless of how complete the rest looks. That
        is the whole point of separating the two gap lists: tennis parses cleanly,
        reports live, and carries both scores -- and is still unmodellable, because
        what it is missing cannot be assumed the way stoppage time can.
        """
        return (
            self.is_live
            and not self.ended
            and not self.blocking_gaps
            and self.home_score is not None
            and self.away_score is not None
        )


class SportRules(Protocol):
    """How to read one sport's feed events and what its match structure is."""

    kind: SportKind
    regulation_seconds: int | None
    periods: int | None

    def parse(self, event: object) -> MatchState | None:
        """Interpret one feed event, or ``None`` if it cannot be read."""
        ...


# --- Shared parsing helpers ----------------------------------------------
_SIMPLE_SCORE = re.compile(r"^\s*(-?\d+)\s*-\s*(-?\d+)\s*$")


def parse_pair(score: str | None) -> tuple[int, int] | None:
    """Parse ``'2-0'`` into ``(2, 0)``.

    ``None`` for anything else, including an empty string and the composite esports
    format. A score that cannot be read must not become 0-0: read as a draw, a 2-0
    lead becomes a coin flip and the model prices a scoreline that never happened.
    """
    if not score:
        return None
    match = _SIMPLE_SCORE.match(score)
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def parse_clock(elapsed: str | None) -> int | None:
    """Parse a clock field into seconds.

    Handles bare minutes (``'23'`` -> 1380) and ``mm:ss`` / ``hh:mm:ss``. Returns
    ``None`` for an empty string, which the feed uses at half time and full time --
    distinct from zero seconds played.
    """
    if not elapsed or not elapsed.strip():
        return None
    parts = elapsed.strip().split(":")
    try:
        numbers = [int(p) for p in parts]
    except ValueError:
        return None
    if len(numbers) == 1:
        return numbers[0] * 60
    if len(numbers) == 2:
        return numbers[0] * 60 + numbers[1]
    if len(numbers) == 3:
        return numbers[0] * 3600 + numbers[1] * 60 + numbers[2]
    return None


def period_number(label: str | None, prefixes: tuple[str, ...]) -> int | None:
    """Extract a 1-based period index from a label like ``'1H'``, ``'Q3'``, ``'S2'``."""
    if not label:
        return None
    text = label.strip().upper()
    for prefix in prefixes:
        pattern = rf"^{re.escape(prefix)}(\d+)$|^(\d+){re.escape(prefix)}$"
        match = re.match(pattern, text)
        if match:
            return int(match.group(1) or match.group(2))
    return None
