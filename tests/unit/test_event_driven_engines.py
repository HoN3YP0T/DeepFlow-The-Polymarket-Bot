"""Event-driven engines: abstention is the normal answer.

These markets have no continuously observable state, so an engine has only two possible
sources for a number — a prior from outside the system, or the market price. The second
is circular and forbidden. So the tests here are mostly about *not* producing an
estimate, and the one hard property is that no path reaches a probability without a
sourced prior.

**Nothing in this system constructs a BaseRate today**, so in production both engines
always abstain. That is the correct behaviour for an engine holding no evidence, and it
is recorded in docs/STATUS.md rather than hidden.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from deepflow.config.thresholds import GeopoliticsThresholds
from deepflow.core.domain import BaseRate, Market, MarketSnapshot, Outcome
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.engines.event_driven import MAX_EVENT_SHIFT
from deepflow.engines.geopolitics.engine import GeopoliticalEngine
from deepflow.engines.geopolitics.events import (
    Direction,
    EventKind,
    EventPipeline,
    GeopoliticalEvent,
    SourceTier,
)
from deepflow.engines.politics.political import POLITICAL_SHIFT_SCALE, PoliticalEngine
from deepflow.engines.registry import EngineRegistry

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
TOKEN = ClobTokenId("11111")
CONDITION = ConditionId("0xcond")


def _market() -> Market:
    return Market(
        condition_id=CONDITION,
        question="Will a ceasefire hold before 2027?",
        outcomes=(Outcome(token_id=TOKEN, label="Yes"),),
        active=True,
        closed=False,
        accepting_orders=True,
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(condition_id=CONDITION, books=(), captured_at=NOW)


def _base_rate(probability: str = "0.40") -> BaseRate:
    return BaseRate(
        probability=Decimal(probability),
        uncertainty=Decimal("0.15"),
        source="test prior",
        as_of=NOW,
    )


def _event(
    *,
    direction: Direction = Direction.ESCALATION,
    kind: EventKind = EventKind.STRIKE,
    severity: int = 100,
    corroborated: int = 2,
    reliability: str = "1",
) -> GeopoliticalEvent:
    return GeopoliticalEvent(
        kind=kind,
        direction=direction,
        severity=severity,
        entities=("Israel", "Lebanon"),
        summary="test",
        sources=(SourceTier.WIRE, SourceTier.OFFICIAL),
        reliability=Decimal(reliability),
        corroborating_source_count=corroborated,
        observed_at=NOW,
    )


def _engine(**kwargs: object) -> GeopoliticalEngine:
    return GeopoliticalEngine(EventPipeline(), GeopoliticsThresholds(), **kwargs)  # type: ignore[arg-type]


async def _estimate(engine: GeopoliticalEngine) -> object:
    return await engine.estimate(market=_market(), snapshot=_snapshot(), token_id=TOKEN)


# --- Abstention -----------------------------------------------------------
@pytest.mark.asyncio
async def test_no_base_rate_means_no_estimate() -> None:
    """The production case. With no prior the only alternatives are the market price,
    which is circular, or a number invented here."""
    engine = _engine()
    engine.observe(_event(), [CONDITION])
    assert await _estimate(engine) is None


@pytest.mark.asyncio
async def test_a_sourced_prior_alone_is_an_estimate() -> None:
    """Changed deliberately on 2026-09-14, and this test records why.

    The engine used to require a corroborated event before it would speak, on the grounds
    that without unpriced evidence the market is probably right. That holds for an engine with
    no information; it does not hold for one an operator has handed a *sourced* prior, which
    is itself the claim "I know something the market may not". Requiring a news event on top
    made the prior unusable — and no news feed exists, while politics is 98 of the 100 markets
    a sweep returns.

    What keeps it honest is downstream: the prior is wide by default, carries its source into
    the journal, and still faces the EV buffer, the Kelly haircut, the gate and risk.
    """
    engine = _engine(base_rates={CONDITION: _base_rate("0.40")})
    estimate = await _estimate(engine)
    assert estimate is not None
    assert estimate.model_probability == Decimal("0.40")
    assert estimate.uncertainty == _base_rate().uncertainty
    assert estimate.inputs["base_rate_source"] == "test prior"


@pytest.mark.asyncio
async def test_a_prior_nobody_has_revisited_in_a_week_is_refused() -> None:
    """A stale judgement is not evidence, and a long-running process would otherwise signal
    on it forever. Refused rather than decayed toward the market: there is no defensible rate
    at which a human judgement becomes a different number by itself."""
    from datetime import timedelta

    from deepflow.core.clock import ManualClock
    from deepflow.engines.event_driven import MAX_PRIOR_AGE

    engine = GeopoliticalEngine(
        EventPipeline(),
        GeopoliticsThresholds(),
        base_rates={CONDITION: _base_rate()},
        clock=ManualClock(NOW + MAX_PRIOR_AGE + timedelta(hours=1)),
    )
    assert await _estimate(engine) is None


@pytest.mark.asyncio
async def test_an_uncorroborated_event_is_not_held_at_all() -> None:
    """Keeping it would let uncorroborated reports accumulate into an estimate nobody decided
    to trust. The prior comes through **unmoved**, which is a stronger statement than the
    abstention this used to assert: it shows the event was discarded rather than merely
    outvoted."""
    engine = _engine(base_rates={CONDITION: _base_rate("0.40")})
    engine.observe(_event(corroborated=1), [CONDITION])
    estimate = await _estimate(engine)
    assert estimate is not None
    assert estimate.model_probability == Decimal("0.40")


@pytest.mark.asyncio
async def test_a_directionless_event_moves_nothing() -> None:
    """An event with no sign is not evidence for either side, so the prior stands."""
    engine = _engine(base_rates={CONDITION: _base_rate("0.40")})
    engine.observe(_event(direction=Direction.NEUTRAL, kind=EventKind.NEGOTIATION), [CONDITION])
    estimate = await _estimate(engine)
    assert estimate is not None
    assert estimate.model_probability == Decimal("0.40")


# --- Estimating -----------------------------------------------------------
@pytest.mark.asyncio
async def test_an_escalation_moves_the_prior_up_and_records_its_source() -> None:
    engine = _engine(base_rates={CONDITION: _base_rate("0.40")})
    engine.observe(_event(), [CONDITION])
    estimate = await _estimate(engine)
    assert estimate is not None
    assert estimate.model_probability == Decimal("0.40") + MAX_EVENT_SHIFT
    assert estimate.inputs["base_rate_source"] == "test prior"
    assert estimate.inputs["base_rate"] == "0.40"


@pytest.mark.asyncio
async def test_a_de_escalation_moves_it_down() -> None:
    engine = _engine(base_rates={CONDITION: _base_rate("0.40")})
    engine.observe(
        _event(direction=Direction.DE_ESCALATION, kind=EventKind.CEASEFIRE_ANNOUNCED), [CONDITION]
    )
    estimate = await _estimate(engine)
    assert estimate is not None
    assert estimate.model_probability == Decimal("0.40") - MAX_EVENT_SHIFT


@pytest.mark.asyncio
async def test_the_shift_is_capped_however_severe_the_event() -> None:
    """The edge is that the market has not priced an event, not that it has mispriced it
    by a distance. A large shift from a keyword-classified news item is not supportable."""
    engine = _engine(base_rates={CONDITION: _base_rate("0.50")})
    engine.observe(_event(severity=100), [CONDITION])
    estimate = await _estimate(engine)
    assert estimate is not None
    assert estimate.model_probability - Decimal("0.50") <= MAX_EVENT_SHIFT


@pytest.mark.asyncio
async def test_many_reports_of_one_escalation_are_one_escalation() -> None:
    """Driven by the most severe event in each direction, not the sum — otherwise
    coverage volume masquerades as evidence."""
    single = _engine(base_rates={CONDITION: _base_rate()})
    single.observe(_event(), [CONDITION])
    one = await _estimate(single)

    many = _engine(base_rates={CONDITION: _base_rate()})
    for _ in range(5):
        many.observe(_event(), [CONDITION])
    five = await _estimate(many)
    assert one is not None and five is not None
    assert one.model_probability == five.model_probability


@pytest.mark.asyncio
async def test_opposing_events_net_off() -> None:
    engine = _engine(base_rates={CONDITION: _base_rate("0.50")})
    engine.observe(_event(severity=80), [CONDITION])
    engine.observe(
        _event(direction=Direction.DE_ESCALATION, kind=EventKind.CEASEFIRE_ANNOUNCED, severity=80),
        [CONDITION],
    )
    estimate = await _estimate(engine)
    assert estimate is not None
    assert estimate.model_probability == Decimal("0.50")  # net zero shift, so the prior stands


@pytest.mark.asyncio
async def test_uncertainty_widens_with_the_shift() -> None:
    """An event-driven adjustment is the least certain estimate this system makes, and
    the uncertainty buffer is what stops it being sized like a measurement."""
    engine = _engine(base_rates={CONDITION: _base_rate()})
    engine.observe(_event(), [CONDITION])
    estimate = await _estimate(engine)
    assert estimate is not None
    assert estimate.uncertainty > _base_rate().uncertainty


@pytest.mark.asyncio
async def test_a_probability_never_leaves_the_unit_interval() -> None:
    engine = _engine(base_rates={CONDITION: _base_rate("0.97")})
    engine.observe(_event(), [CONDITION])
    estimate = await _estimate(engine)
    assert estimate is not None
    assert Decimal(0) <= estimate.model_probability <= Decimal(1)


@pytest.mark.asyncio
async def test_an_event_for_another_market_does_not_reach_this_one() -> None:
    engine = _engine(base_rates={CONDITION: _base_rate("0.40")})
    engine.observe(_event(), [ConditionId("0xelsewhere")])
    estimate = await _estimate(engine)
    assert estimate is not None
    assert estimate.model_probability == Decimal("0.40")


# --- The political variant ------------------------------------------------
@pytest.mark.asyncio
async def test_a_political_market_moves_less_on_the_same_event() -> None:
    """Between scheduled announcements a political market's true probability does not
    drift, so a news item short of the announcement says less than in a conflict."""
    conflict = _engine(base_rates={CONDITION: _base_rate("0.40")})
    conflict.observe(_event(), [CONDITION])
    conflict_estimate = await _estimate(conflict)

    political = PoliticalEngine(EventPipeline(), base_rates={CONDITION: _base_rate("0.40")})
    political.observe(_event(), [CONDITION])
    political_estimate = await political.estimate(
        market=_market(), snapshot=_snapshot(), token_id=TOKEN
    )
    assert conflict_estimate is not None and political_estimate is not None
    shift_conflict = conflict_estimate.model_probability - Decimal("0.40")
    shift_political = political_estimate.model_probability - Decimal("0.40")
    assert shift_political == shift_conflict * POLITICAL_SHIFT_SCALE


@pytest.mark.asyncio
async def test_the_political_engine_abstains_without_a_prior_too() -> None:
    political = PoliticalEngine(EventPipeline())
    political.observe(_event(), [CONDITION])
    assert await political.estimate(market=_market(), snapshot=_snapshot(), token_id=TOKEN) is None


# --- Registration ---------------------------------------------------------
def test_both_engines_can_be_registered_together() -> None:
    """The registry raises on a duplicate category claim, so two engines overlapping is a
    wiring bug that must surface here rather than depending on import order."""
    registry = EngineRegistry()
    registry.register(GeopoliticalEngine(EventPipeline(), GeopoliticsThresholds()))
    registry.register(PoliticalEngine(EventPipeline()))
    assert MarketCategory.POLITICS in registry.registered_categories
    assert MarketCategory.WAR_CONFLICT in registry.registered_categories


def test_the_two_engines_claim_disjoint_categories() -> None:
    geo = GeopoliticalEngine(EventPipeline(), GeopoliticsThresholds())
    political = PoliticalEngine(EventPipeline())
    assert not geo.categories & political.categories


@pytest.mark.asyncio
async def test_a_prior_far_from_the_market_is_treated_as_an_error_not_an_edge() -> None:
    """Added after a verification run approved a 0.79 edge on a prior that was simply wrong.

    A prior of 0.97 typed against a market trading at 0.18 passed the full gate, reporting a
    net EV of 0.78 (§87). Every safeguard behaved correctly and none could help, because
    nothing distinguishes a sourced prior from a fabricated one — `source` is free text. At 25
    points of divergence the likelier explanation is a stale or mistyped prior than a market
    that wrong, and the costs are asymmetric: refusing loses a trade, accepting sizes a
    position at odds nobody checked.
    """
    from deepflow.core.domain import BookLevel, OrderBook

    def _snapshot_at(ask: str) -> MarketSnapshot:
        return MarketSnapshot(
            condition_id=CONDITION,
            books=(
                OrderBook(
                    token_id=TOKEN,
                    bids=(BookLevel(price=Decimal(ask) - Decimal("0.01"), size=Decimal(100)),),
                    asks=(BookLevel(price=Decimal(ask), size=Decimal(100)),),
                    captured_at=NOW,
                ),
            ),
            captured_at=NOW,
        )

    engine = _engine(base_rates={CONDITION: _base_rate("0.97")})
    far = await engine.estimate(market=_market(), snapshot=_snapshot_at("0.18"), token_id=TOKEN)
    assert far is None

    near = await engine.estimate(market=_market(), snapshot=_snapshot_at("0.90"), token_id=TOKEN)
    assert near is not None
    assert near.model_probability == Decimal("0.97")


@pytest.mark.asyncio
async def test_the_divergence_bound_only_ever_suppresses() -> None:
    """It reads the market price, which the independence contract permits only in this
    direction: the contract forbids *deriving* an estimate from the price, and a bound on a
    number we supplied can never create or enlarge a trade."""
    engine = _engine(base_rates={CONDITION: _base_rate("0.40")})
    # No book at all: nothing to bound against, and the estimate is the prior unchanged.
    estimate = await _estimate(engine)
    assert estimate is not None
    assert estimate.model_probability == Decimal("0.40")
