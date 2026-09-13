"""Cricket rules, and the precise reason cricket cannot be modelled in play.

Cricket has been described three different ways in this repository's history, each
wrong in a smaller way than the last. The record matters, because the mistake was
always the same one: testing one source and concluding about the venue.

1. *"No data source"* (§5). False. Cricket markets are listed and accepting orders,
   with teams, provider ids and a resolution source.
2. *"No in-play feed"* (§34). False. A live fixture was observed at
   ``score='74-100'``, ``period='Live'``; finished ones read ``score='151-150'``,
   ``period='FT'``.
3. *"No ``game_id``, so no socket join"* (§49). Half true, and for the wrong reason.
   Cricket **does** carry an id -- ``eventMetadata.gameId`` is
   ``'1000169067LIVE2026'`` -- but it is a *string* in a different place, and the
   SDK's ``EventSportsMetadata.game_id`` is typed ``int | None``, so the field reads
   as absent.

What is actually true is narrower and does not move: the feed gives **runs and the
innings phase, and nothing else**. Per the published vocabulary, cricket's ``period``
is one of ``1H``, ``1A``, ``2H``, ``2A``, ``SO``, ``FT`` -- first and second innings
by side, super over, full time. There is no ``elapsed``, and neither the sports
AsyncAPI spec nor the Gamma OpenAPI spec mentions wickets, overs, balls or innings
counts anywhere.

**Runs alone cannot place a chase.** A side needing 100 with two overs left and one
wicket standing is nearly beaten; the same side needing 100 with ten overs and eight
wickets is comfortable. Identical ``score`` strings, opposite probabilities -- which
is structurally the same defect as tennis's missing set score, and just as
disqualifying. Wickets and balls remaining are both first-order terms in any chase
model, and no assumption substitutes for either.

So this module parses cricket faithfully and declares the two gaps as blocking.
:attr:`MatchState.is_modellable` is therefore ``False``, and ``CricketEngine``
abstains with a named reason rather than being silently absent -- which is what it
was before, on a claim that turned out to be wrong twice over.

One market type deserves its own warning. ``cricket_toss_winner`` prices at exactly
``0.5`` / ``0.5``, because a coin toss is a coin toss: there is no information to
model and the taker fee makes it negative-EV by construction. It is excluded by
:data:`deepflow.adapters.polymarket.games.TRADEABLE_SPORTS_MARKET_TYPES` already,
and named here so nobody adds it later mistaking 0.5 for an opportunity.
"""

from __future__ import annotations

from typing import Final

from deepflow.engines.sports.rules.base import (
    MatchState,
    SportKind,
    parse_pair,
)

LIVE_STATUSES: Final = frozenset({"inprogress", "in_progress", "running", "live"})
ENDED_STATUSES: Final = frozenset({"finished", "cancelled", "canceled", "postponed", "abandoned"})

#: Innings phase -> (innings number, batting side). ``SO`` is a super over, which is
#: a tiebreak rather than a third innings, so it is numbered beyond regulation.
INNINGS_PHASES: Final[dict[str, tuple[int, str]]] = {
    "1H": (1, "home"),
    "1A": (1, "away"),
    "2H": (2, "home"),
    "2A": (2, "away"),
    "SO": (3, "unknown"),
}

#: Regulation innings per side. A super over sits outside it.
INNINGS_TOTAL: Final = 2

#: Both are first-order terms in a chase. Runs without them do not identify the
#: match state at all, so they disqualify rather than widen a band.
BLOCKING: Final = ("wickets_fallen", "balls_remaining")

#: Everything else a cricket model would want. Moot while the two above are missing,
#: recorded so a data-vendor decision can be costed rather than guessed at.
UNAVAILABLE: Final = (
    "overs_bowled",
    "run_rate",
    "required_run_rate",
    "batting_partnership",
    "bowler_figures",
    "powerplay_state",
    "match_format_overs",
    "dls_par_score",
)


def innings_phase(label: object) -> tuple[int | None, str | None]:
    """Split a cricket ``period`` into innings number and batting side.

    ``('2A')`` is the second innings with the away side batting -- which is the
    chase, and the only phase where a target even exists. Unknown labels return
    ``(None, None)`` rather than a guess: ``Live`` is a real observed value that
    names no innings at all.
    """
    key = str(label or "").strip().upper()
    if key not in INNINGS_PHASES:
        return None, None
    number, side = INNINGS_PHASES[key]
    return number, side


class CricketRules:
    """Reads cricket feed events. Parsing succeeds; modelling does not."""

    kind = SportKind.CRICKET
    regulation_seconds: int | None = None
    """Cricket has no clock. A T20 is bounded by balls, a Test by days, and the feed
    sends neither."""
    periods: int | None = INNINGS_TOTAL

    def parse(self, event: object) -> MatchState | None:
        status = str(getattr(event, "status", "") or "").lower()
        runs = parse_pair(getattr(event, "score", None))
        label = getattr(event, "period", None)
        number, _side = innings_phase(label)

        return MatchState(
            sport=SportKind.CRICKET,
            league=str(getattr(event, "league_abbreviation", "") or ""),
            home_team=getattr(event, "home_team", None),
            away_team=getattr(event, "away_team", None),
            # Runs, not wickets and not a target. Shares the field name with every
            # other sport, so the blocking list is what stops a model reading a
            # 151-150 as a one-point lead in a settled game.
            home_score=runs[0] if runs else None,
            away_score=runs[1] if runs else None,
            period_index=number,
            periods_total=INNINGS_TOTAL,
            period_label=str(label) if label else None,
            is_live=bool(getattr(event, "live", False)) or status in LIVE_STATUSES,
            ended=bool(getattr(event, "ended", False)) or status in ENDED_STATUSES,
            unavailable=UNAVAILABLE,
            blocking_gaps=BLOCKING,
        )
