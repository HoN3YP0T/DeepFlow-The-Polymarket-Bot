"""Live game-state models, one per sport. Section 6.

These are the inputs to the sport probability models. They are deliberately
separate from the probability engines: state ingestion (from the sports stream)
and state interpretation (the model) fail differently and are tested apart.

Every field is optional. Live feeds drop fields, and a missing red-card count
must read as "unknown", not as "zero" -- the difference decides whether the
model abstains or trades.

**Provenance warning.** Polymarket's sports stream supplies only a small subset
of what is modelled here: a combined score string, period, elapsed clock, status,
and possession for NFL/CFB. Everything else -- xG, shots, cards, possession
percentage, server, wickets, rally streaks -- must come from a third-party
provider. Two consequences:

* a field left ``None`` because nobody wired a provider is indistinguishable, to
  the model, from a field the feed happened to drop, and both produce an
  abstention. The models will therefore abstain permanently on the venue feed
  alone, which is correct but is a procurement gap, not a bug to fix in code.
* cricket and badminton are absent from the venue feed entirely.

See :mod:`deepflow.adapters.polymarket.sports_feed` for the exact wire payload
and the documented league list.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class GameState(BaseModel):
    """Fields common to every sport."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    home_team: str | None = None
    away_team: str | None = None
    is_live: bool = False
    is_finished: bool = False
    last_updated_age_seconds: float | None = None
    """Staleness of the *game* feed, tracked separately from market data:
    a current order book alongside a two-minute-old score is the single most
    dangerous combination in live sports trading."""


class FootballState(GameState):
    """Section 6, football."""

    home_score: int | None = None
    away_score: int | None = None
    minute: int | None = None
    stoppage_minutes: int | None = None
    home_red_cards: int | None = None
    away_red_cards: int | None = None
    home_yellow_cards: int | None = None
    away_yellow_cards: int | None = None
    possession_home_pct: Decimal | None = None
    home_shots: int | None = None
    away_shots: int | None = None
    home_shots_on_target: int | None = None
    away_shots_on_target: int | None = None
    home_xg: Decimal | None = None
    away_xg: Decimal | None = None
    home_strength: Decimal | None = None
    away_strength: Decimal | None = None
    momentum: Decimal | None = Field(default=None, ge=-1, le=1)

    @property
    def goal_difference(self) -> int | None:
        if self.home_score is None or self.away_score is None:
            return None
        return self.home_score - self.away_score


class CricketState(GameState):
    """Section 6, cricket.

    No venue-native source. Every field here requires an external provider.
    """

    match_format: str | None = None
    """TEST / ODI / T20. Changes the model entirely, not just its parameters."""
    innings: int | None = None
    runs: int | None = None
    wickets: int | None = None
    overs_completed: Decimal | None = None
    balls_remaining: int | None = None
    target: int | None = None
    runs_required: int | None = None
    wickets_remaining: int | None = None
    current_run_rate: Decimal | None = None
    required_run_rate: Decimal | None = None
    partnership_runs: int | None = None
    batting_strength: Decimal | None = None
    bowling_strength: Decimal | None = None


class TennisState(GameState):
    """Section 6, tennis."""

    sets_home: int | None = None
    sets_away: int | None = None
    games_home: int | None = None
    games_away: int | None = None
    points_home: str | None = None
    points_away: str | None = None
    server_is_home: bool | None = None
    """Serve dominates short-horizon tennis probability. Without it the model
    abstains rather than guessing."""
    break_points_home: int | None = None
    break_points_away: int | None = None
    is_deuce: bool | None = None
    advantage_home: bool | None = None
    is_tiebreak: bool | None = None
    is_set_point: bool | None = None
    is_match_point: bool | None = None
    home_serve_win_pct: Decimal | None = None
    home_return_win_pct: Decimal | None = None


class BadmintonState(GameState):
    """Section 6, badminton.

    No venue-native source. Every field here requires an external provider.
    """

    games_home: int | None = None
    games_away: int | None = None
    points_home: int | None = None
    points_away: int | None = None
    is_game_point: bool | None = None
    is_match_point: bool | None = None
    rally_streak: int | None = None
    streak_is_home: bool | None = None
    momentum: Decimal | None = Field(default=None, ge=-1, le=1)

    @property
    def point_differential(self) -> int | None:
        if self.points_home is None or self.points_away is None:
            return None
        return self.points_home - self.points_away
