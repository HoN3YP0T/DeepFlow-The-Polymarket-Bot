"""The market <-> live-game join, tested against captured venue payloads.

These tests exist because the opposite claim -- that no join key exists -- was
committed to ``docs/STATUS.md`` as a blocker code could not solve. The fixture
is real ``list_events`` output captured on 2026-09-13, so a venue change that
breaks the join breaks these tests rather than silently disabling a strategy.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from polymarket.models.gamma.event import Event

from deepflow.adapters.polymarket.games import (
    TRADEABLE_SPORTS_MARKET_TYPES,
    GameLink,
    links_from_events,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "sports_game_join.json"


@pytest.fixture(scope="module")
def payloads() -> dict[str, list[dict[str, Any]]]:
    return json.loads(FIXTURE.read_text())


def _events(raw: list[dict[str, Any]]) -> list[Event]:
    return [Event.model_validate(item) for item in raw]


def test_the_join_key_lives_on_the_event(payloads: dict[str, Any]) -> None:
    """Every sports event carries a sports block, and most carry a game id.

    Not all: cricket arrives with live state and no game id (see
    :func:`test_cricket_has_live_state_without_a_game_id`), which is why the fold
    key falls back to the event id rather than requiring the field.
    """
    events = _events(payloads["live_events"])
    assert events
    assert all(event.sports is not None for event in events)
    assert any(event.sports is not None and event.sports.game_id for event in events)


def test_market_game_id_is_a_different_id_space(payloads: dict[str, Any]) -> None:
    """Guards the mistake itself, not just its fix.

    ``market.sports.game_id`` is not the fixture id. On a fixture-level market it
    is ``None``; on a ``child_moneyline`` it identifies the child contest -- game
    1 of a series -- and never equals its own fixture's id. Querying
    ``list_markets(game_id=<fixture id>)`` therefore returns nothing while
    looking exactly right, which is how the join came to be called impossible.
    """
    events = _events(payloads["live_events"])
    checked = 0
    for event in events:
        fixture_id = event.sports.game_id if event.sports else None
        for market in event.markets:
            market_game_id = market.sports.game_id if market.sports else None
            if market_game_id is None:
                continue
            checked += 1
            assert str(market_game_id) != str(fixture_id)
    assert checked, "fixture should include a child market carrying its own game id"


def test_one_fixture_folds_into_one_link(payloads: dict[str, Any]) -> None:
    """Nine event families, one game.

    Iterating events rather than folding them reports the same fixture nine
    times, each with identical in-play state -- nine game states where there is
    one game.
    """
    events = _events(payloads["one_fixture_many_events"])
    assert len(events) > 1
    links = links_from_events(events)

    assert len(links) == 1
    link = next(iter(links.values()))
    assert link.game_id == 90112380
    assert link.fixture_key == "90112380"
    assert len(link.event_ids) == len(events)
    # The fixture's own slug, not a market-family suffix of it.
    assert link.slug == "fl1-str-asm-2026-09-12"


def test_fold_keeps_markets_from_every_event_family(payloads: dict[str, Any]) -> None:
    events = _events(payloads["one_fixture_many_events"])
    expected = sum(len(event.markets) for event in events)
    link = next(iter(links_from_events(events).values()))
    assert len(link.markets) == expected


def test_folded_markets_carry_their_event_id(payloads: dict[str, Any]) -> None:
    """A nested market's own ``events`` list is empty, so the id must be attached.

    Without this the join resolves a fixture to markets that cannot be traced
    back to it, which is the join failing one step later.
    """
    events = _events(payloads["one_fixture_many_events"])
    link = next(iter(links_from_events(events).values()))
    assert link.markets
    assert all(market.event_id is not None for market in link.markets)
    assert set(link.event_ids) >= {market.event_id for market in link.markets}


def test_suspended_fixture_is_not_in_play(payloads: dict[str, Any]) -> None:
    """``live=True`` is not a statement that the game is being played.

    The captured fixture is a Chile Primera game flagged live, with period
    ``SUS`` and a kickoff days in the future. Pricing it as in-play means
    modelling a game nobody is playing.
    """
    events = _events(payloads["suspended_but_live"])
    links = links_from_events(events)
    assert links
    for link in links.values():
        assert link.live is True
        assert link.period == "SUS"
        assert link.is_in_play is False


def test_in_play_requires_kickoff_to_have_passed() -> None:
    """A future kickoff contradicts any claim of live play."""
    from datetime import UTC, datetime, timedelta

    base = {
        "fixture_key": "1",
        "game_id": 1,
        "event_ids": (),
        "slug": "x",
        "title": "x",
        "league_tags": (),
        "markets": (),
        "live": True,
        "period": "Q1",
    }
    future = GameLink(**base, start_time=datetime.now(UTC) + timedelta(hours=2))
    past = GameLink(**base, start_time=datetime.now(UTC) - timedelta(hours=1))
    unknown = GameLink(**base)

    assert future.is_in_play is False
    assert past.is_in_play is True
    # No kickoff time is not evidence against live play.
    assert unknown.is_in_play is True


def test_ended_fixture_is_not_in_play() -> None:
    link = GameLink(
        fixture_key="1",
        game_id=1,
        event_ids=(),
        slug="x",
        title="x",
        league_tags=(),
        markets=(),
        live=True,
        ended=True,
        period="FT",
    )
    assert link.is_in_play is False


def test_tradeable_markets_are_restricted_to_modellable_types(
    payloads: dict[str, Any],
) -> None:
    """A 189-market football fixture yields the handful we can actually price.

    The venue publishes 240 sports market types; this system prices two. The
    filter is the difference between trading a moneyline and guessing at
    ``anytime_touchdowns``.
    """
    events = _events(payloads["live_events"])
    links = links_from_events(events)
    assert links

    for link in links.values():
        assert all(
            market.sports_market_type in TRADEABLE_SPORTS_MARKET_TYPES
            for market in link.tradeable_markets
        )
        assert all(
            market.accepting_orders and market.enable_order_book and not market.closed
            for market in link.tradeable_markets
        )

    # At least one captured fixture must actually have something tradeable, or
    # the filter is passing by rejecting everything.
    assert any(link.tradeable_markets for link in links.values())


def test_cricket_keeps_its_id_even_though_it_is_not_numeric(
    payloads: dict[str, Any],
) -> None:
    """Cricket has live state *and* an id -- the id is a string, somewhere else.

    Three wrong conclusions were drawn about cricket in turn: no data source, then no
    in-play feed, then no game id. The last is the subtlest: the typed
    ``sports.game_id`` really is ``None``, because the venue puts cricket's id in
    ``eventMetadata.gameId`` as a string (``'id2705330274512646'`` here,
    ``'1000169067LIVE2026'`` on another fixture -- the shapes are not even
    consistent) and the SDK types the numeric field ``int | None``.

    So reading only the typed field concludes the sport is unidentifiable. The
    fixture key must use whatever the venue published, and the numeric field stays
    typed for the socket join, which only ever sends integers.
    """
    events = _events(payloads["live_events"])
    cricket = [event for event in events if event.slug.startswith("cr")]
    assert cricket, "fixture should include the captured cricket fixture"

    for event in cricket:
        assert event.sports is not None
        assert event.sports.score
        # The trap: typed field empty, id present in the metadata blob.
        assert event.sports.game_id is None
        assert (event.metadata or {}).get("gameId")

    links = links_from_events(cricket)
    assert len(links) == len(cricket)
    for link in links.values():
        assert link.game_id is None, "not numeric, so not socket-joinable"
        assert link.provider_game_id, "but the venue did publish an id"
        assert not link.fixture_key.startswith("event:"), "so the key is that id"
        assert link.score


def test_provider_game_id_prefers_the_numeric_field(payloads: dict[str, Any]) -> None:
    """Where both exist, the typed id wins -- it is the one the socket sends."""
    events = [e for e in _events(payloads["live_events"]) if not e.slug.startswith("cr")]
    assert events
    for link in links_from_events(events).values():
        assert link.game_id is not None
        assert link.provider_game_id == str(link.game_id)


def test_cricket_parses_but_is_not_modellable(payloads: dict[str, Any]) -> None:
    """Runs and an innings phase are readable; a chase cannot be placed from them.

    100 needed with two overs and one wicket is nearly lost; 100 needed with ten
    overs and eight wickets is comfortable. The feed says the same thing in both
    cases, so the gaps are blocking rather than degrading -- the same structural
    defect as tennis's missing set score.
    """
    from deepflow.engines.sports.rules import SportKind, SportRegistry
    from deepflow.engines.sports.rules.cricket import BLOCKING, CricketRules

    registry = SportRegistry()
    cricket = [e for e in _events(payloads["live_events"]) if e.slug.startswith("cr")]
    link = next(iter(links_from_events(cricket).values()))

    # Offline resolution cannot read "Live" as cricket -- it names no innings. The
    # tag tier is what identifies the sport; this asserts it does not guess wrong.
    assert registry.sport_for(link) in {SportKind.CRICKET, SportKind.UNKNOWN}

    state = CricketRules().parse(link)
    assert state is not None
    assert state.home_score is not None and state.away_score is not None
    assert set(BLOCKING) == {"wickets_fallen", "balls_remaining"}
    assert state.is_modellable is False


def test_non_sports_events_are_skipped_not_grouped() -> None:
    """An event with no game id must not become a fixture.

    ``sports_market_types`` is silently ignored by the events endpoint -- it
    returns the whole catalogue rather than an error -- so non-sports events do
    reach this code when a caller trusts that filter.
    """

    class _NotASportsEvent:
        id = "1"
        slug = "kraken-ipo-in-2025"
        title = "Kraken IPO"
        sports = None
        tags = ()
        markets = ()
        state = None
        schedule = None

    assert links_from_events([_NotASportsEvent()]) == {}


# --- The seam: a GameLink must be readable by the sport rules -----------------
#
# These exist because both halves were written, tested and verified separately
# and then connected to nothing. A test per half passes while the pipeline has a
# probability layer it cannot feed, so the join is what needs asserting.


def test_league_abbreviation_comes_from_the_slug(payloads: dict[str, Any]) -> None:
    """The registry resolves on a league code, and the slug already carries it."""
    links = links_from_events(_events(payloads["live_events"]))
    codes = {link.league_abbreviation for link in links.values()}
    assert codes == {"cfb", "lol", "crint"}


def test_sport_registry_reads_a_game_link_directly(payloads: dict[str, Any]) -> None:
    """A ``GameLink`` quacks like a feed event, so no second translation layer.

    Resolution here is offline: the venue league list is not loaded, so this also
    pins that the payload-shape tier works on REST-sourced fixtures and not only on
    socket payloads.
    """
    from deepflow.engines.sports.rules import SportKind, SportRegistry

    registry = SportRegistry()
    by_code = {
        link.league_abbreviation: registry.sport_for(link)
        for link in links_from_events(_events(payloads["live_events"])).values()
    }

    assert by_code["lol"] is SportKind.ESPORTS
    assert by_code["cfb"] is SportKind.AMERICAN_FOOTBALL


def test_captured_fixtures_parse_into_match_state(payloads: dict[str, Any]) -> None:
    from deepflow.engines.sports.rules import SportRegistry

    registry = SportRegistry()
    parsed = {
        link.league_abbreviation: registry.parse(link)
        for link in links_from_events(_events(payloads["live_events"])).values()
    }

    # Esports: maps won, not rounds -- the composite score's middle field.
    esports = parsed["lol"]
    assert esports is not None
    assert (esports.home_score, esports.away_score) == (1, 1)

    # Gridiron: Q4 with a countdown clock inside the quarter.
    gridiron = parsed["cfb"]
    assert gridiron is not None
    assert gridiron.period_index == 4


def test_soccer_fixture_parses_from_the_rest_sweep(payloads: dict[str, Any]) -> None:
    """Offline soccer resolution, on a REST fixture rather than a socket payload."""
    from deepflow.engines.sports.rules import SportKind, SportRegistry

    registry = SportRegistry()
    link = next(iter(links_from_events(_events(payloads["one_fixture_many_events"])).values()))
    assert registry.sport_for(link) is SportKind.SOCCER


def test_category_mapping_does_not_route_gridiron_into_soccer() -> None:
    """``MarketCategory.FOOTBALL`` means soccer, and the map must respect that.

    Mapping ``AMERICAN_FOOTBALL`` by name would hand every NFL and college
    football fixture the soccer strategy's thresholds and its 90-minute clock.
    """
    from deepflow.core.enums import MarketCategory
    from deepflow.engines.sports.rules import SportKind, category_for

    assert category_for(SportKind.SOCCER) is MarketCategory.FOOTBALL
    assert category_for(SportKind.AMERICAN_FOOTBALL) is MarketCategory.OTHER_SPORTS
    assert category_for(SportKind.CRICKET) is MarketCategory.CRICKET
    # An unresolved sport has no category, rather than defaulting into a real one.
    assert category_for(SportKind.UNKNOWN) is None
