"""Event pipeline: reliability, corroboration, and matching against resolution text.

These markets move violently on claims retracted within the hour, so the pipeline's job
is mostly refusal. Three refusals carry the weight:

* an unverified social post carries **zero** weight and produces no event at all;
* one report is a report — two *independent publishers* are evidence;
* an event can be real, relevant, and still not bear on a market whose payout condition
  names different actors or excludes that class of event outright.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.config.thresholds import GeopoliticsThresholds
from deepflow.core.clock import ManualClock
from deepflow.core.domain import ResolutionCriteria
from deepflow.core.enums import ResolutionValidity
from deepflow.core.types import ConditionId
from deepflow.engines.geopolitics.events import (
    CORROBORATION_WINDOW,
    Direction,
    EventKind,
    EventPipeline,
    GeopoliticalEvent,
    RawReport,
    SourceTier,
    claim_fingerprint,
    classify_kind,
    extract_entities,
)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
MARKET = ConditionId("0xmarket")


def _report(
    *,
    publisher: str = "Reuters",
    tier: SourceTier = SourceTier.WIRE,
    headline: str = "Israel launches airstrike on Lebanon border",
    body: str = "",
    published_at: datetime | None = None,
    hints: tuple[str, ...] = (),
) -> RawReport:
    return RawReport(
        publisher=publisher,
        tier=tier,
        headline=headline,
        body=body,
        published_at=published_at or NOW,
        entity_hints=hints,
    )


def _pipeline(*, clock: ManualClock | None = None, **thresholds: object) -> EventPipeline:
    return EventPipeline(
        GeopoliticsThresholds(**thresholds),  # type: ignore[arg-type]
        clock=clock or ManualClock(NOW),
    )


def _criteria(
    *,
    yes: str | None = "Israel and Lebanon agree a ceasefire before 2027",
    qualifying: tuple[str, ...] = (),
    non_qualifying: tuple[str, ...] = (),
    validity: ResolutionValidity = ResolutionValidity.VALID,
) -> ResolutionCriteria:
    return ResolutionCriteria(
        validity=validity,
        yes_condition=yes,
        qualifying_events=qualifying,
        non_qualifying_events=non_qualifying,
    )


# --- Source weighting -----------------------------------------------------
@pytest.mark.asyncio
async def test_an_unverified_social_post_produces_no_event() -> None:
    """Weight zero, and therefore nothing to corroborate with. A bot that trades an
    unconfirmed strike report is systematically selling liquidity to whoever waited."""
    event = await _pipeline().ingest(_report(tier=SourceTier.SOCIAL_UNVERIFIED))
    assert event is None


@pytest.mark.asyncio
async def test_social_weight_is_configurable_but_still_not_confirmation() -> None:
    """Even weighted, one post is one source — it cannot reach the corroboration bar
    alone."""
    pipeline = _pipeline(unverified_social_weight=Decimal("0.3"))
    event = await pipeline.ingest(_report(tier=SourceTier.SOCIAL_UNVERIFIED))
    assert event is not None
    assert not event.is_actionable


@pytest.mark.asyncio
async def test_reliability_is_the_best_source_not_the_average() -> None:
    """An average would *lower* a claim's reliability as more outlets picked it up, and a
    sum would let three local reports outrank an official statement."""
    pipeline = _pipeline()
    await pipeline.ingest(_report(publisher="Local Daily", tier=SourceTier.LOCAL_MEDIA))
    event = await pipeline.ingest(_report(publisher="Ministry", tier=SourceTier.OFFICIAL))
    assert event is not None
    assert event.reliability == Decimal("1.00")


# --- Corroboration --------------------------------------------------------
@pytest.mark.asyncio
async def test_one_report_is_not_actionable() -> None:
    event = await _pipeline().ingest(_report())
    assert event is not None
    assert event.corroborating_source_count == 1
    assert not event.is_actionable


@pytest.mark.asyncio
async def test_a_non_actionable_event_is_still_emitted() -> None:
    """The first credible report of a strike is real information and belongs in the
    journal. Suppressing it would make the second report look like the first."""
    event = await _pipeline().ingest(_report())
    assert event is not None
    assert event.kind is EventKind.STRIKE


@pytest.mark.asyncio
async def test_two_independent_publishers_make_it_actionable() -> None:
    pipeline = _pipeline()
    await pipeline.ingest(_report(publisher="Reuters"))
    event = await pipeline.ingest(_report(publisher="AP"))
    assert event is not None
    assert event.corroborating_source_count == 2
    assert event.is_actionable


@pytest.mark.asyncio
async def test_the_same_publisher_repeating_itself_is_not_corroboration() -> None:
    """Two stories from one newsroom are one witness. Counting them would let a single
    outlet corroborate itself."""
    pipeline = _pipeline()
    await pipeline.ingest(_report(publisher="Reuters"))
    event = await pipeline.ingest(_report(publisher="Reuters", body="updated"))
    assert event is not None
    assert event.corroborating_source_count == 1
    assert not event.is_actionable


@pytest.mark.asyncio
async def test_corroboration_is_counted_on_publishers_not_tiers() -> None:
    pipeline = _pipeline()
    await pipeline.ingest(_report(publisher="Reuters", tier=SourceTier.WIRE))
    event = await pipeline.ingest(_report(publisher="AP", tier=SourceTier.WIRE))
    assert event is not None
    assert event.is_actionable


@pytest.mark.asyncio
async def test_reports_outside_the_window_are_two_stories_not_two_witnesses() -> None:
    """Two reports a week apart are not two witnesses to one event, and treating them as
    corroboration lets a slow drip of coverage clear the bar."""
    clock = ManualClock(NOW)
    pipeline = _pipeline(clock=clock)
    await pipeline.ingest(_report(publisher="Reuters"))

    later = ManualClock(NOW + CORROBORATION_WINDOW + timedelta(minutes=1))
    stale = EventPipeline(GeopoliticsThresholds(), clock=later)
    stale._claims = pipeline._claims  # same window, advanced clock
    event = await stale.ingest(_report(publisher="AP"))
    assert event is not None
    assert event.corroborating_source_count == 1


# --- Classification -------------------------------------------------------
def test_a_ceasefire_collapse_is_not_read_as_an_announcement() -> None:
    """ "ceasefire failed" contains "ceasefire", so a shortest-first scan inverts the
    direction of the resulting probability shift."""
    assert classify_kind("Ceasefire failed as shelling resumed") is EventKind.CEASEFIRE_FAILED
    assert classify_kind("Ceasefire announced in Geneva") is EventKind.CEASEFIRE_ANNOUNCED


def test_an_unrecognised_claim_is_other_not_a_guess() -> None:
    assert classify_kind("Trade delegation discusses agricultural tariffs") is EventKind.OTHER


@pytest.mark.asyncio
async def test_an_ambiguous_kind_carries_no_direction() -> None:
    """A negotiation can precede a deal or a collapse. Putting a sign on it would invent
    one, so the engine treats NEUTRAL as no shift."""
    event = await _pipeline().ingest(_report(headline="Summit talks open in Doha"))
    assert event is not None
    assert event.kind is EventKind.NEGOTIATION
    assert event.direction is Direction.NEUTRAL


@pytest.mark.asyncio
async def test_severity_is_scaled_by_reliability() -> None:
    """A credible local report of an invasion matters more than an official statement
    about an ambassador; a severity ignoring reliability ranks them backwards."""
    pipeline = _pipeline()
    official = await pipeline.ingest(
        _report(publisher="Ministry", tier=SourceTier.OFFICIAL, headline="Invasion begins")
    )
    local = await pipeline.ingest(
        _report(publisher="Local", tier=SourceTier.LOCAL_MEDIA, headline="Invasion begins")
    )
    assert official is not None and local is not None
    assert official.severity > local.severity


# --- Entities -------------------------------------------------------------
def test_feed_hints_come_before_prose() -> None:
    """A feed that resolved its own entities is better evidence than a regex over
    English."""
    entities = extract_entities("Fighting continues", hints=("Israel", "Lebanon"))
    assert entities[:2] == ("Israel", "Lebanon")


def test_sentence_openers_are_not_entities() -> None:
    """Without stopwords a capitalised-word scan turns every sentence opener into a
    country."""
    entities = extract_entities("After the strike, Reuters reported that Iran responded")
    assert "After" not in entities
    assert "Iran" in entities


def test_a_claim_fingerprint_ignores_wording() -> None:
    """Two wires describing one strike will not share phrasing, and a fingerprint
    sensitive to it would count them as separate claims and never corroborate."""
    first = claim_fingerprint(EventKind.STRIKE, ("Israel", "Lebanon"))
    second = claim_fingerprint(EventKind.STRIKE, ("lebanon", "israel"))
    assert first == second


# --- Market matching ------------------------------------------------------
def _event(
    *,
    kind: EventKind = EventKind.CEASEFIRE_ANNOUNCED,
    entities: tuple[str, ...] = ("Israel", "Lebanon"),
) -> GeopoliticalEvent:
    return GeopoliticalEvent(
        kind=kind,
        direction=Direction.DE_ESCALATION,
        severity=80,
        entities=entities,
        summary="ceasefire agreed",
        sources=(SourceTier.WIRE, SourceTier.OFFICIAL),
        reliability=Decimal(1),
        corroborating_source_count=2,
        observed_at=NOW,
    )


@pytest.mark.asyncio
async def test_a_market_naming_the_same_actors_matches() -> None:
    matched = await _pipeline().map_to_markets(_event(), {MARKET: _criteria()})
    assert matched == (MARKET,)


@pytest.mark.asyncio
async def test_a_market_about_other_actors_does_not_match() -> None:
    """Geopolitical markets are dense with shared vocabulary — "strike", "talks",
    "ceasefire" appear in dozens of unrelated questions. The actors are what make an
    event *this* market's business."""
    other = _criteria(yes="Russia and Ukraine agree a ceasefire before 2027")
    assert await _pipeline().map_to_markets(_event(), {MARKET: other}) == ()


@pytest.mark.asyncio
async def test_an_unparsed_market_is_never_matched() -> None:
    """Guessing from its title is exactly what matching on resolution text avoids."""
    unparsed = _criteria(validity=ResolutionValidity.AMBIGUOUS)
    assert await _pipeline().map_to_markets(_event(), {MARKET: unparsed}) == ()


@pytest.mark.asyncio
async def test_a_non_qualifying_clause_excludes_the_market_outright() -> None:
    """The venue telling us a category of event does not count overrides any entity
    overlap — the market pays on a narrower question than "did X happen"."""
    excluded = _criteria(non_qualifying=("A ceasefire announced by either party",))
    assert await _pipeline().map_to_markets(_event(), {MARKET: excluded}) == ()


@pytest.mark.asyncio
async def test_an_event_with_no_entities_matches_nothing() -> None:
    assert await _pipeline().map_to_markets(_event(entities=()), {MARKET: _criteria()}) == ()
