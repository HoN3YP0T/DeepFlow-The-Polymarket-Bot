"""The sports feed's wire shape, and what it does not carry."""

from __future__ import annotations

import pytest

from deepflow.adapters.polymarket.sports_feed import (
    UNSOURCED_CATEGORIES,
    SportsFeedEvent,
)
from deepflow.core.enums import MarketCategory


def _event(**overrides: object) -> SportsFeedEvent:
    payload: dict[str, object] = {
        "game_id": 5127839,
        "league_abbreviation": "NBA",
        "status": "InProgress",
        "live": True,
        "ended": False,
        "score": "98-94",
        "period": "Q4",
        "elapsed": "05:12",
    }
    payload.update(overrides)
    return SportsFeedEvent(**payload)  # type: ignore[arg-type]


def test_score_is_one_combined_string() -> None:
    """The feed sends ``"<home>-<away>"``, not two numeric fields."""
    assert _event().scores() == (98, 94)


@pytest.mark.parametrize("score", ["", "98", "-", "TBD", "98-94-90", "abc-def"])
def test_unparseable_score_is_unknown_not_zero(score: str) -> None:
    """A missing score must not read as 0-0.

    Read as a draw, a 2-0 lead becomes a coin flip and the model happily prices
    a scoreline that never happened -- the exact failure the optional-field
    convention in ``GameState`` exists to prevent.
    """
    assert _event(score=score).scores() is None


def test_elapsed_parses_a_clock() -> None:
    assert _event(elapsed="05:12").elapsed_seconds() == 312
    assert _event(elapsed="1:05:12").elapsed_seconds() == 3912


@pytest.mark.parametrize("elapsed", [None, "", "n/a", "Q4"])
def test_elapsed_without_a_clock_is_none(elapsed: str | None) -> None:
    """Sports with no running clock leave this unusable; ``None`` must not be
    read as zero minutes played."""
    assert _event(elapsed=elapsed).elapsed_seconds() is None


def test_possession_is_absent_outside_nfl_and_cfb() -> None:
    assert _event().turn is None


def test_only_badminton_has_no_live_state_source_anywhere() -> None:
    """Cricket came out of this set on 2026-09-13, and must not go back in.

    The socket has no cricket vocabulary, which is what put it here. But a live
    international fixture was observed through Gamma's event index with a score
    and a period, so "no venue-native source" was the wrong conclusion --
    ``CricketEngine`` was disabled on it. Badminton stays only because no such
    observation exists for it yet.
    """
    assert frozenset({MarketCategory.BADMINTON}) == UNSOURCED_CATEGORIES
    assert MarketCategory.CRICKET not in UNSOURCED_CATEGORIES


def test_unknown_fields_do_not_break_parsing() -> None:
    """The model accepts extras: a new feed field should not crash ingestion."""
    event = _event(some_new_field="x")
    assert event.game_id == 5127839
