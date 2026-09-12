"""Per-sport feed interpretation, against a real captured feed.

``tests/fixtures/sports_feed_capture.json`` holds live payloads from 22 leagues.
These tests exist because the feed sends the same eight fields for every sport and
those fields mean different things per sport -- reading one with another's rules
produces a confident wrong answer rather than an error.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from deepflow.engines.sports.rules import MODELLABLE_SPORTS, SportRegistry
from deepflow.engines.sports.rules.base import SportKind, parse_clock, parse_pair
from deepflow.engines.sports.rules.esports import EsportsRules
from deepflow.engines.sports.rules.gridiron import QUARTER_SECONDS, GridironRules
from deepflow.engines.sports.rules.soccer import REGULATION_SECONDS, SoccerRules
from deepflow.engines.sports.rules.tennis import TennisRules

CAPTURE = Path(__file__).parent.parent / "fixtures" / "sports_feed_capture.json"


@pytest.fixture(scope="module")
def capture() -> dict[str, list[dict[str, Any]]]:
    return json.loads(CAPTURE.read_text())


@pytest.fixture
def registry() -> SportRegistry:
    return SportRegistry()


def _event(**kw: Any) -> SimpleNamespace:
    base = {
        "league_abbreviation": "nor",
        "status": "InProgress",
        "score": "2-0",
        "period": "1H",
        "elapsed": "23",
        "live": True,
        "ended": False,
        "home_team": "Rosenborg BK",
        "away_team": "Tromso IL",
    }
    base.update(kw)
    return SimpleNamespace(**base)


# --- Every captured league resolves --------------------------------------
def test_every_captured_league_resolves_to_a_sport(
    registry: SportRegistry, capture: dict[str, list[dict[str, Any]]]
) -> None:
    """A league resolving to UNKNOWN is not modelled, so an unresolved league is
    silently lost coverage."""
    unresolved = [
        league
        for league, events in capture.items()
        if registry.sport_for(SimpleNamespace(**events[0])) is SportKind.UNKNOWN
    ]
    assert unresolved == []


def test_captured_leagues_cover_four_sports(
    registry: SportRegistry, capture: dict[str, list[dict[str, Any]]]
) -> None:
    kinds = {registry.sport_for(SimpleNamespace(**e[0])) for e in capture.values()}
    assert {
        SportKind.SOCCER,
        SportKind.AMERICAN_FOOTBALL,
        SportKind.TENNIS,
        SportKind.ESPORTS,
    } <= kinds


# --- Soccer ---------------------------------------------------------------
def test_soccer_clock_counts_up(registry: SportRegistry) -> None:
    """``elapsed='23'`` is 23 minutes played, so 67 remain of regulation."""
    state = registry.parse(_event(league_abbreviation="nor", elapsed="23", period="1H"))
    assert state is not None
    assert state.sport is SportKind.SOCCER
    assert state.seconds_elapsed == 23 * 60
    assert state.seconds_remaining == REGULATION_SECONDS - 23 * 60


def test_soccer_score_is_goals(registry: SportRegistry) -> None:
    state = registry.parse(_event(score="0-2"))
    assert state is not None
    assert (state.home_score, state.away_score) == (0, 2)
    assert state.score_difference == -2


def test_soccer_half_time_has_no_clock_but_a_known_remainder(
    registry: SportRegistry,
) -> None:
    """The feed sends ``elapsed=''`` at HT. That is unknown, not zero -- and the
    remainder is still knowable from the half."""
    state = registry.parse(_event(period="HT", elapsed="", status="Break"))
    assert state is not None
    assert state.seconds_elapsed is None
    assert state.seconds_remaining == 45 * 60
    assert state.is_break


def test_soccer_full_time_is_ended(registry: SportRegistry) -> None:
    state = registry.parse(_event(period="FT", elapsed="", status="Final", live=False))
    assert state is not None
    assert state.ended and not state.is_modellable


def test_soccer_stoppage_is_assumed_not_read(registry: SportRegistry) -> None:
    """The feed has no stoppage indicator, so it has to be assumed -- and the
    assumption is larger for the second half, where the late-game strategy lives."""
    rules = SoccerRules()
    assert rules.expected_stoppage_seconds(2) > 0
    assert rules.expected_stoppage_seconds(1) > rules.expected_stoppage_seconds(2)
    state = registry.parse(_event())
    assert state is not None and "stoppage_time" in state.unavailable


# --- American football: the clock trap -----------------------------------
def test_gridiron_clock_counts_down_within_the_quarter(
    registry: SportRegistry,
) -> None:
    """The trap this module exists for. ``'05:04'`` at Q1 is five minutes *remaining*
    in the quarter -- about 50 left in the game. Read as soccer-style minutes played
    it would say 85 remaining."""
    state = registry.parse(
        _event(
            league_abbreviation="cfb",
            score="0-10",
            period="Q1",
            elapsed="05:04",
            status="inprogress",
        )
    )
    assert state is not None
    assert state.sport is SportKind.AMERICAN_FOOTBALL
    assert state.seconds_remaining == 5 * 60 + 4 + 3 * QUARTER_SECONDS
    assert state.seconds_elapsed is not None
    assert state.seconds_elapsed < 11 * 60


def test_gridiron_final_quarter_has_no_quarters_after_it(
    registry: SportRegistry,
) -> None:
    state = registry.parse(
        _event(league_abbreviation="nfl", period="Q4", elapsed="02:00", status="inprogress")
    )
    assert state is not None and state.seconds_remaining == 120


def test_soccer_and_gridiron_read_the_same_string_differently(
    registry: SportRegistry,
) -> None:
    """The point of per-sport rules, stated as a test."""
    soccer = registry.parse(_event(league_abbreviation="nor", elapsed="10", period="1H"))
    gridiron = registry.parse(
        _event(league_abbreviation="cfb", elapsed="10:00", period="Q1", status="inprogress")
    )
    assert soccer is not None and gridiron is not None
    assert soccer.seconds_remaining == 80 * 60
    assert gridiron.seconds_remaining == 10 * 60 + 3 * QUARTER_SECONDS


# --- Tennis: parses, does not model --------------------------------------
def test_tennis_score_is_games_in_the_current_set(registry: SportRegistry) -> None:
    """Observed live: a wta match went 2-1 then 2-3 while period stayed S1."""
    state = registry.parse(
        _event(
            league_abbreviation="wta", score="2-3", period="S1", elapsed=None, status="inprogress"
        )
    )
    assert state is not None
    assert state.sport is SportKind.TENNIS
    assert (state.home_score, state.away_score) == (2, 3)
    assert state.period_index == 1


def test_tennis_is_not_modellable_despite_parsing_cleanly(
    registry: SportRegistry,
) -> None:
    """The distinction the two gap lists exist for. Tennis reports live, carries both
    scores, and is still unmodellable: the set score is not sent, so the same payload
    describes a match nearly won and one nearly lost."""
    state = registry.parse(
        _event(
            league_abbreviation="grand slam",
            score="2-3",
            period="S3",
            elapsed=None,
            status="inprogress",
        )
    )
    assert state is not None
    assert state.is_live
    assert state.home_score is not None
    assert not state.is_modellable
    assert "sets_won_by_each_player" in state.blocking_gaps


def test_tennis_match_length_is_also_unknown(registry: SportRegistry) -> None:
    """Best-of-3 versus best-of-5 is not in the feed either."""
    assert TennisRules().periods is None
    state = registry.parse(
        _event(
            league_abbreviation="atp", score="0-0", period="S1", elapsed=None, status="inprogress"
        )
    )
    assert state is not None and state.periods_total is None


# --- Esports: the composite score ---------------------------------------
def test_esports_score_is_maps_not_rounds(registry: SportRegistry) -> None:
    """``'2-0|0-1|Bo5'`` is a player *behind* in the series while ahead on the current
    map. Reading the first field as the series score inverts the meaning."""
    state = registry.parse(
        _event(
            league_abbreviation="lol",
            score="2-0|0-1|Bo5",
            period="2/5",
            elapsed=None,
            status="running",
        )
    )
    assert state is not None
    assert state.sport is SportKind.ESPORTS
    assert (state.home_score, state.away_score) == (0, 1)
    assert state.periods_total == 5
    assert state.period_index == 2


def test_esports_handles_zero_padded_rounds(registry: SportRegistry) -> None:
    state = registry.parse(
        _event(
            league_abbreviation="cs2",
            score="000-000|1-1|Bo3",
            period="3/3",
            elapsed=None,
            status="running",
        )
    )
    assert state is not None and (state.home_score, state.away_score) == (1, 1)


def test_esports_series_target_is_known_not_assumed(registry: SportRegistry) -> None:
    """The one thing this feed gives that soccer's does not."""
    assert EsportsRules.maps_to_win(3) == 2
    assert EsportsRules.maps_to_win(5) == 3
    assert EsportsRules.maps_to_win(1) == 1


def test_non_composite_score_is_not_parsed_as_esports() -> None:
    assert EsportsRules().parse(_event(score="2-0")) is None


# --- Resolution tiers ----------------------------------------------------
async def test_venue_extends_the_league_map() -> None:
    """``get_sports()`` resolves a league the explicit map has never heard of."""

    class _Fake:
        async def get_sports(self) -> list[Any]:
            return [
                SimpleNamespace(sport="newleague", tags="1,100639,100350"),  # soccer
                SimpleNamespace(sport="anothercric", tags="1,517"),  # cricket
            ]

    registry = SportRegistry()
    before = registry.leagues_known
    added = await registry.load_from_venue(_Fake())
    assert added == 2
    assert registry.leagues_known == before + 2
    assert registry.sport_for(_event(league_abbreviation="newleague")) is SportKind.SOCCER


async def test_explicit_mapping_wins_over_the_venue() -> None:
    """The explicit entries exist precisely where the venue's tags are known wrong,
    so the venue must not overwrite them."""

    class _Fake:
        async def get_sports(self) -> list[Any]:
            return [SimpleNamespace(sport="nfl", tags="1,100350")]  # claims soccer

    registry = SportRegistry()
    await registry.load_from_venue(_Fake())
    assert registry.sport_for(_event(league_abbreviation="nfl")) is SportKind.AMERICAN_FOOTBALL


async def test_venue_failure_keeps_the_explicit_map() -> None:
    class _Broken:
        async def get_sports(self) -> list[Any]:
            raise RuntimeError("gamma down")

    registry = SportRegistry()
    assert await registry.load_from_venue(_Broken()) == 0
    assert registry.sport_for(_event(league_abbreviation="wta")) is SportKind.TENNIS


def test_payload_shape_is_the_last_resort(registry: SportRegistry) -> None:
    """An unknown league with a composite score is still unambiguously esports."""
    assert (
        registry.sport_for(_event(league_abbreviation="brandnew", score="0-0|1-0|Bo3"))
        is SportKind.ESPORTS
    )


def test_unknown_league_with_no_signature_is_unknown(registry: SportRegistry) -> None:
    """Guessing is how a lacrosse game gets priced with a soccer model."""
    assert (
        registry.sport_for(
            _event(league_abbreviation="ncaalax", score="7-5", period="P3", elapsed=None)
        )
        is SportKind.UNKNOWN
    )
    assert registry.parse(_event(league_abbreviation="ncaalax", period="P3")) is None


def test_modellable_sports_excludes_tennis_and_cricket() -> None:
    assert SportKind.TENNIS not in MODELLABLE_SPORTS
    assert SportKind.CRICKET not in MODELLABLE_SPORTS
    assert SportKind.SOCCER in MODELLABLE_SPORTS


# --- Shared helpers -------------------------------------------------------
@pytest.mark.parametrize("raw", ["", None, "2-", "abc", "0-0|1-0|Bo3", "1-2-3"])
def test_unparseable_score_is_none_not_zero(raw: str | None) -> None:
    """Read as 0-0, a 2-0 lead becomes a coin flip and the model prices a scoreline
    that never happened."""
    assert parse_pair(raw) is None


@pytest.mark.parametrize(("raw", "expected"), [("23", 1380), ("05:04", 304), ("1:05:00", 3900)])
def test_clock_parsing(raw: str, expected: int) -> None:
    assert parse_clock(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", None, "n/a"])
def test_empty_clock_is_unknown(raw: str | None) -> None:
    assert parse_clock(raw) is None


def test_gridiron_rules_declare_their_structure() -> None:
    assert GridironRules().periods == 4
    assert SoccerRules().periods == 2
