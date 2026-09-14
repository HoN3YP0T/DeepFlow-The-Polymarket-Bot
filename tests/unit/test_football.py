"""The football model, its join, and every way it declines to answer.

The arithmetic is checked against football reality rather than against itself: a
one-goal lead with five minutes left is worth about 0.94, and a model that disagrees
with that is wrong however elegant its derivation.

The abstentions get more tests than the arithmetic, because each one is a way the
engine could instead have produced a confident number about the wrong thing --
and the worst of them, picking the wrong member of a three-way result group, prices
the exact complement of the intended trade.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.config.thresholds import Thresholds
from deepflow.core.domain import Market, MarketSnapshot, Outcome
from deepflow.core.enums import DataQuality, OutcomeSide
from deepflow.engines.sports.football import (
    MIN_UNCERTAINTY,
    STRENGTH_UNCERTAINTY,
    FootballEngine,
)
from deepflow.engines.sports.live_state import MAX_STATE_AGE, MatchStateStore
from deepflow.engines.sports.rules.base import MatchState, SportKind
from deepflow.engines.sports.scoreline import result_probabilities

NOW = datetime(2026, 9, 14, 20, tzinfo=UTC)
HOME = "CD Coquimbo Unido"
AWAY = "CD Huachipato"
TOKEN = "tok-yes"


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


def _state(
    *,
    home: int | None = 1,
    away: int | None = 0,
    remaining: int | None = 5 * 60,
    half: int | None = 2,
    live: bool = True,
    ended: bool = False,
    sport: SportKind = SportKind.SOCCER,
    blocking: tuple[str, ...] = (),
) -> MatchState:
    return MatchState(
        sport=sport,
        league="chi1",
        home_score=home,
        away_score=away,
        period_index=half,
        periods_total=2,
        period_label="2H",
        seconds_elapsed=(90 * 60 - remaining) if remaining is not None else None,
        seconds_remaining=remaining,
        is_live=live,
        ended=ended,
        blocking_gaps=blocking,
    )


def _market(
    *,
    title: str | None = HOME,
    market_type: str | None = "moneyline",
    condition_id: str = "0xaa",
) -> Market:
    return Market(
        condition_id=condition_id,  # type: ignore[arg-type]
        question=f"Will {title} win?",
        slug="chi1-ccu-cdh-2026-09-14-ccu",
        group_item_title=title,
        outcomes=(
            Outcome(token_id=TOKEN, label="Yes", side=OutcomeSide.YES),  # type: ignore[arg-type]
            Outcome(token_id="tok-no", label="No", side=OutcomeSide.NO),  # type: ignore[arg-type]
        ),
        active=True,
        closed=False,
        accepting_orders=True,
        sports_market_type=market_type,
    )


def _snapshot(condition_id: str = "0xaa") -> MarketSnapshot:
    return MarketSnapshot(
        condition_id=condition_id,  # type: ignore[arg-type]
        captured_at=NOW,
        books=(),
        quality=DataQuality.FRESH,
    )


def _engine(
    state: MatchState | None = None,
    *,
    clock: _Clock | None = None,
    home: str = HOME,
    away: str = AWAY,
    condition_id: str = "0xaa",
) -> FootballEngine:
    clock = clock or _Clock()
    store = MatchStateStore(clock)  # type: ignore[arg-type]
    if state is not None:
        store.observe((condition_id,), state, home_team=home, away_team=away)  # type: ignore[arg-type]
    return FootballEngine(Thresholds().sports.football, states=store)


async def _estimate(engine: FootballEngine, market: Market):
    return await engine.estimate(
        market=market,
        snapshot=_snapshot(str(market.condition_id)),
        token_id=TOKEN,  # type: ignore[arg-type]
    )


# --- the arithmetic, against football reality ----------------------------
@pytest.mark.parametrize(
    ("home", "away", "minutes_left", "expected", "tolerance"),
    [
        # A one-goal lead in the closing minutes. Bookmakers price this in the low
        # 0.90s; a model that says 0.99 here is the overconfidence calibration exists
        # to catch, and one that says 0.80 has not noticed the clock.
        (1, 0, 5, 0.94, 0.04),
        (2, 0, 15, 0.97, 0.03),
        # Level at half time is close to the pre-match draw-inclusive split.
        (0, 0, 45, 0.35, 0.06),
        (1, 0, 45, 0.74, 0.06),
    ],
)
def test_the_distribution_matches_football_reality(
    home: int, away: int, minutes_left: int, expected: float, tolerance: float
) -> None:
    result = result_probabilities(
        home_score=home, away_score=away, seconds_remaining=minutes_left * 60
    )
    assert float(result.home) == pytest.approx(expected, abs=tolerance)


def test_the_three_results_always_sum_to_one() -> None:
    """A distribution that leaks probability reads downstream as an edge."""
    for home, away, remaining in ((0, 0, 5400), (3, 1, 600), (0, 2, 60), (1, 1, 0)):
        result = result_probabilities(home_score=home, away_score=away, seconds_remaining=remaining)
        assert result.home + result.draw + result.away == pytest.approx(
            Decimal(1), abs=Decimal("0.000001")
        )


def test_a_finished_match_is_a_certainty_not_a_near_one() -> None:
    """The boundary worth pinning: smeared probability on a settled match is how a
    model buys a 0.99 that is really a 1.00 -- or a 0.00."""
    settled = result_probabilities(home_score=1, away_score=0, seconds_remaining=0)
    assert settled.home == Decimal(1)
    assert settled.draw == Decimal(0)
    assert settled.away == Decimal(0)

    drawn = result_probabilities(home_score=2, away_score=2, seconds_remaining=0)
    assert drawn.draw == Decimal(1)


def test_a_lead_is_worth_more_as_the_clock_runs_down() -> None:
    """The core shape: what remains is the count of realistic chances, not minutes."""
    values = [
        result_probabilities(home_score=1, away_score=0, seconds_remaining=m * 60).home
        for m in (45, 30, 15, 5, 1)
    ]
    assert values == sorted(values)


# --- the engine ----------------------------------------------------------
async def test_it_prices_the_home_market() -> None:
    """1-0 with five minutes on the clock prices at 0.893, not the 0.94 the raw
    distribution gives for five minutes.

    The difference is the assumed stoppage: the engine adds five second-half minutes
    the feed never sends, so it prices ten minutes of football rather than five. Five
    points of probability rest on a number nobody observed, which is why the engine
    measures that gap and charges it to the uncertainty rather than hiding it.
    """
    estimate = await _estimate(_engine(_state()), _market(title=HOME))
    assert estimate is not None
    assert estimate.inputs["result"] == "HOME"
    assert estimate.model_probability == pytest.approx(Decimal("0.893"), abs=Decimal("0.01"))
    # ...and the raw five-minute figure is the higher one the stoppage discounts.
    assert (
        result_probabilities(home_score=1, away_score=0, seconds_remaining=5 * 60).home
        > estimate.model_probability
    )


async def test_it_prices_the_away_market_as_the_other_side() -> None:
    estimate = await _estimate(_engine(_state()), _market(title=AWAY))
    assert estimate is not None
    assert estimate.inputs["result"] == "AWAY"
    assert estimate.model_probability < Decimal("0.05")


async def test_the_draw_market_is_recognised_before_the_team_names() -> None:
    """**The ordering that matters.**

    The draw market's title contains *both* team names -- "Draw (A vs. B)" -- so a
    substring test against the home name matches it, and the engine would price a draw
    as a home win. At 1-0 with five minutes left that is 0.94 against 0.06: not an
    approximation, the complement.
    """
    estimate = await _estimate(_engine(_state()), _market(title=f"Draw ({HOME} vs. {AWAY})"))
    assert estimate is not None
    assert estimate.inputs["result"] == "DRAW"
    assert estimate.model_probability < Decimal("0.1")


async def test_the_three_markets_of_a_group_price_to_one() -> None:
    """Each is a separate binary market and together they are one distribution."""
    engine = _engine(_state(home=0, away=0, remaining=20 * 60))
    total = Decimal(0)
    for title in (HOME, f"Draw ({HOME} vs. {AWAY})", AWAY):
        estimate = await _estimate(engine, _market(title=title))
        assert estimate is not None
        total += estimate.model_probability
    assert total == pytest.approx(Decimal(1), abs=Decimal("0.000001"))


async def test_an_unrecognised_entity_abstains_rather_than_guessing() -> None:
    """No safe fallback exists: the results are mutually exclusive, so picking wrong
    prices the exact complement of the intended trade."""
    assert await _estimate(_engine(_state()), _market(title="Both teams to score")) is None


async def test_a_market_with_no_entity_abstains() -> None:
    assert await _estimate(_engine(_state()), _market(title=None)) is None


async def test_only_the_full_time_result_market_is_answered() -> None:
    """The same fixture lists half-time and second-half families. Who leads at half
    time is a different question, and a full-time distribution answers it confidently
    and wrongly."""
    assert await _estimate(_engine(_state()), _market(market_type="soccer_halftime_result")) is None


async def test_a_market_with_no_live_state_abstains() -> None:
    assert await _estimate(_engine(None), _market()) is None


async def test_a_stale_score_abstains() -> None:
    """A current book beside a two-minute-old score is the most dangerous combination
    in live sports trading: the book has already moved on the goal the model cannot
    see, so the stale edge reads as a large one."""
    clock = _Clock()
    engine = _engine(_state(), clock=clock)
    assert await _estimate(engine, _market()) is not None

    clock.advance(MAX_STATE_AGE + timedelta(seconds=1))
    assert await _estimate(engine, _market()) is None


async def test_a_fixture_that_is_not_live_abstains() -> None:
    assert await _estimate(_engine(_state(live=False)), _market()) is None


async def test_a_finished_fixture_abstains() -> None:
    assert await _estimate(_engine(_state(ended=True)), _market()) is None


async def test_a_missing_score_abstains_rather_than_reading_nil_nil() -> None:
    """A missing score is not 0-0. Read as a draw, a 2-0 lead becomes a coin flip."""
    assert await _estimate(_engine(_state(home=None)), _market()) is None


async def test_an_unreadable_clock_abstains_rather_than_reading_zero() -> None:
    """The feed sends an empty ``elapsed`` at half time and full time, and zero would
    price the interval as though the match were over."""
    assert await _estimate(_engine(_state(remaining=None)), _market()) is None


async def test_another_sport_is_refused_even_with_a_usable_looking_state() -> None:
    """An esports map score parses into the same fields and means something else
    entirely."""
    assert await _estimate(_engine(_state(sport=SportKind.ESPORTS)), _market()) is None


async def test_a_blocking_gap_disqualifies_however_complete_the_rest_looks() -> None:
    assert await _estimate(_engine(_state(blocking=("set_score",))), _market()) is None


# --- uncertainty ---------------------------------------------------------
async def test_uncertainty_shrinks_as_the_match_runs_out() -> None:
    """The missing team strength is the dominant error at kickoff and nearly gone by
    the 85th minute, because by then the scoreline has decided most of the question."""
    early = await _estimate(_engine(_state(home=0, away=0, remaining=80 * 60, half=1)), _market())
    late = await _estimate(_engine(_state(home=1, away=0, remaining=3 * 60)), _market())
    assert early is not None and late is not None
    assert early.uncertainty > late.uncertainty


async def test_uncertainty_at_kickoff_reflects_the_missing_team_ratings() -> None:
    """A title favourite and a relegation side are priced identically by this model,
    and the number has to say so."""
    estimate = await _estimate(
        _engine(_state(home=0, away=0, remaining=90 * 60, half=1)), _market()
    )
    assert estimate is not None
    assert estimate.uncertainty >= STRENGTH_UNCERTAINTY


async def test_uncertainty_is_never_understated_at_the_death() -> None:
    """Claiming near-certainty would let Kelly size a late number as a sure thing."""
    estimate = await _estimate(_engine(_state(home=3, away=0, remaining=60)), _market())
    assert estimate is not None
    assert estimate.uncertainty >= MIN_UNCERTAINTY


async def test_the_stoppage_assumption_is_charged_to_uncertainty() -> None:
    """Stoppage is assumed, not observed, and at 87' an extra five minutes is a ~40%
    increase in the chances remaining. A tight late game must carry more uncertainty
    than a settled one at the same clock."""
    tight = await _estimate(_engine(_state(home=1, away=0, remaining=60)), _market())
    settled = await _estimate(_engine(_state(home=4, away=0, remaining=60)), _market())
    assert tight is not None and settled is not None
    assert tight.uncertainty > settled.uncertainty


async def test_the_estimate_records_what_it_was_built_from() -> None:
    """A journal row has to be readable months later without the code."""
    estimate = await _estimate(_engine(_state()), _market())
    assert estimate is not None
    assert estimate.inputs["score"] == "1-0"
    assert estimate.inputs["home_team"] == HOME
    assert estimate.inputs["away_team"] == AWAY
    assert int(estimate.inputs["seconds_remaining"]) > 5 * 60  # stoppage included
    assert estimate.inputs["calibration_supported"] == "False"


# --- the store -----------------------------------------------------------
def test_the_store_reports_how_many_markets_it_holds() -> None:
    """Zero while fixtures are in play means the join is broken rather than that
    nothing is on -- the failure `games.py` sat in for two phases."""
    store = MatchStateStore(_Clock())  # type: ignore[arg-type]
    store.observe(("a", "b", "c"), _state(), home_team=HOME, away_team=AWAY)  # type: ignore[arg-type]
    assert store.tracked() == 3


def test_a_forgotten_market_is_gone() -> None:
    store = MatchStateStore(_Clock())  # type: ignore[arg-type]
    store.observe(("a",), _state(), home_team=HOME, away_team=AWAY)  # type: ignore[arg-type]
    store.forget("a")  # type: ignore[arg-type]
    assert store.get("a") is None  # type: ignore[arg-type]
