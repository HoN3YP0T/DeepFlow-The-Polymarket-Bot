"""Geopolitical event pipeline. Section 14.

    Source -> Validate -> Extract -> Entities -> Classify -> Severity
           -> Escalation/de-escalation -> Map to markets -> Reprice -> EV

Source reliability is weighted, and unverified social media carries weight zero
by default. This is not conservatism for its own sake: these markets move
violently on claims that are retracted within the hour, and a bot that trades
an unconfirmed strike report is systematically selling liquidity to whoever
waits for confirmation.

Corroboration is required before an event is actionable -- one reliable source
is a report, two independent ones are evidence.

**No source adapter feeds this pipeline.** There is no news ingestion in this system:
Gamma serves market metadata, the RTDS carries prices, comments and sports, and none of
them carry wire copy. So in production nothing calls :meth:`EventPipeline.ingest`, both
event-driven engines abstain, and that is the correct behaviour rather than a gap being
papered over -- an engine with no evidence should have no opinion. The pipeline is
written and tested so that adding a feed is a wiring job rather than a design one, and
the absence is recorded in ``docs/STATUS.md`` instead of hidden behind a module that
looks connected.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from deepflow.config.thresholds import GeopoliticsThresholds
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import ResolutionCriteria
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId

log = get_logger(__name__)


class EventKind(StrEnum):
    MILITARY_ESCALATION = "MILITARY_ESCALATION"
    STRIKE = "STRIKE"
    CEASEFIRE_ANNOUNCED = "CEASEFIRE_ANNOUNCED"
    CEASEFIRE_FAILED = "CEASEFIRE_FAILED"
    NEGOTIATION = "NEGOTIATION"
    SANCTIONS = "SANCTIONS"
    ELECTION_RESULT = "ELECTION_RESULT"
    OFFICIAL_ANNOUNCEMENT = "OFFICIAL_ANNOUNCEMENT"
    DIPLOMATIC_DEVELOPMENT = "DIPLOMATIC_DEVELOPMENT"
    TERRITORIAL_CHANGE = "TERRITORIAL_CHANGE"
    LEADERSHIP_CHANGE = "LEADERSHIP_CHANGE"
    ENERGY_DISRUPTION = "ENERGY_DISRUPTION"
    OTHER = "OTHER"


class Direction(StrEnum):
    ESCALATION = "ESCALATION"
    DE_ESCALATION = "DE_ESCALATION"
    NEUTRAL = "NEUTRAL"


class SourceTier(StrEnum):
    """Reliability tiers. Weights are configured, not hardcoded here."""

    OFFICIAL = "OFFICIAL"
    """Government, military or institutional statement."""
    WIRE = "WIRE"
    """Established wire services."""
    ESTABLISHED_MEDIA = "ESTABLISHED_MEDIA"
    LOCAL_MEDIA = "LOCAL_MEDIA"
    SOCIAL_UNVERIFIED = "SOCIAL_UNVERIFIED"
    """Weight zero by default. Never treated as confirmation."""


class GeopoliticalEvent(BaseModel):
    """A validated, classified event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: EventKind
    direction: Direction
    severity: int = Field(ge=0, le=100)
    entities: tuple[str, ...] = ()
    """Countries, organizations, named actors."""
    summary: str
    sources: tuple[SourceTier, ...] = ()
    reliability: Decimal = Field(ge=0, le=1)
    corroborating_source_count: int = Field(default=0, ge=0)
    observed_at: datetime
    affected_markets: tuple[ConditionId, ...] = ()

    @property
    def is_actionable(self) -> bool:
        """Whether the event may move a probability at all.

        Independent corroboration is required. A single report -- however
        reliable the outlet -- is not confirmation of a fast-moving claim.
        """
        return self.corroborating_source_count >= 2 and self.reliability > 0


#: Default reliability by source tier.
#:
#: ``SOCIAL_UNVERIFIED`` is zero, and that is a trading decision rather than editorial
#: caution: these markets move violently on claims retracted within the hour, so a bot
#: that trades an unconfirmed strike report is systematically selling liquidity to
#: whoever waited for confirmation. Overridable via
#: ``GeopoliticsThresholds.unverified_social_weight``, which is also zero by default.
TIER_RELIABILITY: Final[dict[SourceTier, Decimal]] = {
    SourceTier.OFFICIAL: Decimal("1.00"),
    SourceTier.WIRE: Decimal("0.95"),
    SourceTier.ESTABLISHED_MEDIA: Decimal("0.85"),
    SourceTier.LOCAL_MEDIA: Decimal("0.65"),
    SourceTier.SOCIAL_UNVERIFIED: Decimal(0),
}

#: How long a claim stays in the corroboration window.
#:
#: Corroboration is a *time-bounded* question. Two reports a week apart are two stories,
#: not two witnesses to one event, and treating them as corroboration would let a slow
#: drip of coverage clear the bar that exists to require independent confirmation.
CORROBORATION_WINDOW = timedelta(hours=6)

#: Keyword -> event kind. Ordered longest-first at match time so "ceasefire failed"
#: cannot be read as "ceasefire announced".
KIND_KEYWORDS: Final[tuple[tuple[str, EventKind], ...]] = (
    ("ceasefire collapse", EventKind.CEASEFIRE_FAILED),
    ("ceasefire fails", EventKind.CEASEFIRE_FAILED),
    ("ceasefire failed", EventKind.CEASEFIRE_FAILED),
    ("ceasefire broken", EventKind.CEASEFIRE_FAILED),
    ("violates ceasefire", EventKind.CEASEFIRE_FAILED),
    ("ceasefire agreed", EventKind.CEASEFIRE_ANNOUNCED),
    ("ceasefire announced", EventKind.CEASEFIRE_ANNOUNCED),
    ("truce agreed", EventKind.CEASEFIRE_ANNOUNCED),
    ("airstrike", EventKind.STRIKE),
    ("air strike", EventKind.STRIKE),
    ("missile strike", EventKind.STRIKE),
    ("drone strike", EventKind.STRIKE),
    ("shelling", EventKind.STRIKE),
    ("invasion", EventKind.MILITARY_ESCALATION),
    ("mobilisation", EventKind.MILITARY_ESCALATION),
    ("mobilization", EventKind.MILITARY_ESCALATION),
    ("troops deployed", EventKind.MILITARY_ESCALATION),
    ("sanction", EventKind.SANCTIONS),
    ("embargo", EventKind.SANCTIONS),
    ("talks", EventKind.NEGOTIATION),
    ("negotiation", EventKind.NEGOTIATION),
    ("summit", EventKind.NEGOTIATION),
    ("peace deal", EventKind.NEGOTIATION),
    ("election result", EventKind.ELECTION_RESULT),
    ("wins election", EventKind.ELECTION_RESULT),
    ("concedes", EventKind.ELECTION_RESULT),
    ("resigns", EventKind.LEADERSHIP_CHANGE),
    ("ousted", EventKind.LEADERSHIP_CHANGE),
    ("steps down", EventKind.LEADERSHIP_CHANGE),
    ("coup", EventKind.LEADERSHIP_CHANGE),
    ("annex", EventKind.TERRITORIAL_CHANGE),
    ("captures", EventKind.TERRITORIAL_CHANGE),
    ("withdraws from", EventKind.TERRITORIAL_CHANGE),
    ("pipeline", EventKind.ENERGY_DISRUPTION),
    ("refinery", EventKind.ENERGY_DISRUPTION),
    ("oil terminal", EventKind.ENERGY_DISRUPTION),
    ("official statement", EventKind.OFFICIAL_ANNOUNCEMENT),
    ("ministry says", EventKind.OFFICIAL_ANNOUNCEMENT),
    ("ambassador", EventKind.DIPLOMATIC_DEVELOPMENT),
    ("diplomatic", EventKind.DIPLOMATIC_DEVELOPMENT),
)

#: Which direction each kind pushes a conflict market.
#:
#: ``NEUTRAL`` where the kind genuinely does not say: a negotiation can precede a deal
#: or a collapse, and guessing a direction for it would put a sign on evidence that has
#: none. The engine treats NEUTRAL as "no probability shift", so an unclear event is
#: recorded without moving anything.
KIND_DIRECTION: Final[dict[EventKind, Direction]] = {
    EventKind.MILITARY_ESCALATION: Direction.ESCALATION,
    EventKind.STRIKE: Direction.ESCALATION,
    EventKind.CEASEFIRE_FAILED: Direction.ESCALATION,
    EventKind.TERRITORIAL_CHANGE: Direction.ESCALATION,
    EventKind.ENERGY_DISRUPTION: Direction.ESCALATION,
    EventKind.CEASEFIRE_ANNOUNCED: Direction.DE_ESCALATION,
    EventKind.NEGOTIATION: Direction.NEUTRAL,
    EventKind.SANCTIONS: Direction.ESCALATION,
    EventKind.ELECTION_RESULT: Direction.NEUTRAL,
    EventKind.LEADERSHIP_CHANGE: Direction.NEUTRAL,
    EventKind.OFFICIAL_ANNOUNCEMENT: Direction.NEUTRAL,
    EventKind.DIPLOMATIC_DEVELOPMENT: Direction.NEUTRAL,
    EventKind.OTHER: Direction.NEUTRAL,
}

#: Base severity by kind, before the reliability haircut.
KIND_SEVERITY: Final[dict[EventKind, int]] = {
    EventKind.MILITARY_ESCALATION: 85,
    EventKind.STRIKE: 75,
    EventKind.CEASEFIRE_FAILED: 80,
    EventKind.CEASEFIRE_ANNOUNCED: 80,
    EventKind.TERRITORIAL_CHANGE: 70,
    EventKind.LEADERSHIP_CHANGE: 65,
    EventKind.ELECTION_RESULT: 60,
    EventKind.ENERGY_DISRUPTION: 55,
    EventKind.SANCTIONS: 45,
    EventKind.NEGOTIATION: 35,
    EventKind.OFFICIAL_ANNOUNCEMENT: 30,
    EventKind.DIPLOMATIC_DEVELOPMENT: 25,
    EventKind.OTHER: 10,
}

#: Words that never identify an entity, so a naive capitalised-word scan does not turn
#: every sentence opener into a country.
_ENTITY_STOPWORDS: Final = frozenset(
    {
        "The",
        "A",
        "An",
        "But",
        "And",
        "After",
        "Before",
        "On",
        "In",
        "At",
        "By",
        "For",
        "From",
        "This",
        "That",
        "These",
        "Those",
        "It",
        "Its",
        "Was",
        "Were",
        "Has",
        "Have",
        "Had",
        "Will",
        "Would",
        "Said",
        "Says",
        "According",
        "Reuters",
        "Breaking",
        "Update",
        "Report",
        "Reports",
        "Official",
        "Officials",
        "Sources",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
        "Sunday",
    }
)

_CAPITALISED = re.compile(r"\b([A-Z][a-zA-Z]{2,}(?:\s+[A-Z][a-zA-Z]{2,})?)\b")
_WORD = re.compile(r"[a-z0-9]+")


class RawReport(BaseModel):
    """One incoming claim, before validation.

    ``publisher`` is what independence is counted on, not ``tier``: two wire services are
    two witnesses, while two stories from the same outlet are one. Counting tiers instead
    would let a single newsroom corroborate itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    publisher: str
    tier: SourceTier
    headline: str
    body: str = ""
    url: str | None = None
    published_at: datetime
    entity_hints: tuple[str, ...] = ()
    """Entities the feed already resolved, if any. Merged with what the text yields --
    a feed that names them is more reliable than a regex over prose."""


class EventPipeline:
    """Ingests sources and emits validated events.

    Holds a short corroboration window in memory, because corroboration cannot be judged
    from one report: a single item never knows whether anyone else has said the same
    thing. Deliberately **not** persisted -- an event whose corroboration only exists in
    a six-hour window has nothing to say after a restart, and reloading stale claims
    would let yesterday's reports corroborate today's.
    """

    def __init__(
        self,
        thresholds: GeopoliticsThresholds | None = None,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._thresholds = thresholds or GeopoliticsThresholds()
        self._clock = clock or SystemClock()
        self._claims: dict[str, list[tuple[datetime, str, SourceTier]]] = {}

    async def ingest(self, raw: RawReport) -> GeopoliticalEvent | None:
        """Run one raw item through the pipeline.

        Source -> validate -> extract -> entities -> classify -> severity -> direction,
        with corroboration counted across the window.

        Returns an event even when it is **not yet actionable**, and that is the point:
        the first credible report of a strike is real information about the world and
        belongs in the journal and on the dashboard, it just may not move a probability.
        Suppressing it entirely would make the second report look like the first.

        Returns ``None`` only when the source carries no weight at all -- an unverified
        social post at the default weight of zero. There is nothing to corroborate *with*
        in that case, so recording it as evidence would misrepresent what is held.
        """
        reliability = self._reliability(raw.tier)
        if reliability <= 0:
            log.info(
                "events.source_carries_no_weight",
                publisher=raw.publisher,
                tier=str(raw.tier),
            )
            return None

        text = f"{raw.headline}. {raw.body}".strip()
        kind = classify_kind(text)
        entities = extract_entities(text, hints=raw.entity_hints)
        fingerprint = claim_fingerprint(kind, entities)

        publishers, tiers = self._corroborate(fingerprint, raw)
        return GeopoliticalEvent(
            kind=kind,
            direction=KIND_DIRECTION.get(kind, Direction.NEUTRAL),
            severity=self._severity(kind, reliability),
            entities=entities,
            summary=raw.headline.strip(),
            sources=tiers,
            reliability=self._aggregate_reliability(tiers),
            corroborating_source_count=publishers,
            observed_at=raw.published_at,
        )

    async def map_to_markets(
        self, event: GeopoliticalEvent, markets: Mapping[ConditionId, ResolutionCriteria]
    ) -> tuple[ConditionId, ...]:
        """Find markets whose resolution criteria the event bears on.

        Matched against **parsed resolution criteria**, not market titles. An event can be
        real, relevant, and still not satisfy the specific qualifying condition a market
        pays out on -- "did X happen by date D, as confirmed by source S" is a narrower
        question than "did X happen", and a title match answers the wrong one.

        Three rules, each of which removes a class of false positive:

        * **Only markets whose criteria parsed as valid.** An unparsed market cannot be
          matched against, and guessing from its title is exactly what this method exists
          to avoid.
        * **A non-qualifying event excludes the market outright.** Resolution text that
          says a category of event does *not* count is the venue telling us this evidence
          is irrelevant here, and it overrides any entity overlap.
        * **An entity in common is required**, not merely a keyword. Geopolitical markets
          are dense with shared vocabulary -- "strike", "talks", "ceasefire" appear in
          dozens of unrelated questions -- and the actors are what make an event *this*
          market's business.
        """
        if not event.entities:
            return ()

        matched: list[ConditionId] = []
        for condition_id, criteria in markets.items():
            if not criteria.is_valid:
                continue
            if self._excluded_by(criteria, event):
                log.info(
                    "events.market_excludes_event",
                    condition_id=str(condition_id),
                    kind=str(event.kind),
                )
                continue
            if self._entities_overlap(criteria, event.entities):
                matched.append(condition_id)
        return tuple(matched)

    # --- Corroboration ----------------------------------------------------
    def _corroborate(self, fingerprint: str, raw: RawReport) -> tuple[int, tuple[SourceTier, ...]]:
        """Record this report and return how many *distinct publishers* now back the claim.

        The window is pruned on every call rather than on a timer: a claim that ages out
        while nothing is being ingested is a claim nobody is reporting, and there is no
        decision waiting on it.
        """
        now = self._clock.now()
        cutoff = now - CORROBORATION_WINDOW
        seen = [entry for entry in self._claims.get(fingerprint, []) if entry[0] >= cutoff]

        # Re-reporting by the same publisher is not corroboration. Keeping the newest
        # sighting preserves the claim's freshness without inflating the count.
        seen = [entry for entry in seen if entry[1] != raw.publisher]
        seen.append((now, raw.publisher, raw.tier))
        self._claims[fingerprint] = seen

        return len(seen), tuple(entry[2] for entry in seen)

    def _reliability(self, tier: SourceTier) -> Decimal:
        if tier is SourceTier.SOCIAL_UNVERIFIED:
            return self._thresholds.unverified_social_weight
        return TIER_RELIABILITY.get(tier, Decimal(0))

    def _aggregate_reliability(self, tiers: tuple[SourceTier, ...]) -> Decimal:
        """Reliability of the claim, taken as the **best** source backing it.

        Not a sum and not an average. A sum would let three local reports outrank an
        official statement, and an average would *lower* a claim's reliability as more
        outlets picked it up -- both wrong. Corroboration is counted separately, which is
        where breadth belongs.
        """
        if not tiers:
            return Decimal(0)
        return max(self._reliability(tier) for tier in tiers)

    @staticmethod
    def _severity(kind: EventKind, reliability: Decimal) -> int:
        """Base severity for the kind, scaled by how reliable the report is.

        Scaling rather than gating: a credible local report of an invasion is more
        important than an official statement about an ambassador, and a severity that
        ignored reliability would rank them the other way round.
        """
        return int(Decimal(KIND_SEVERITY.get(kind, 10)) * reliability)

    # --- Market matching --------------------------------------------------
    @staticmethod
    def _excluded_by(criteria: ResolutionCriteria, event: GeopoliticalEvent) -> bool:
        """Whether the market's own text says this class of event does not count."""
        kind_words = set(_WORD.findall(str(event.kind).lower().replace("_", " ")))
        for phrase in criteria.non_qualifying_events:
            if kind_words & set(_WORD.findall(phrase.lower())):
                return True
        return False

    @staticmethod
    def _entities_overlap(criteria: ResolutionCriteria, entities: tuple[str, ...]) -> bool:
        """Whether any of the event's actors appear in the market's payout condition.

        Reads the parsed conditions and qualifying events -- the text the market actually
        pays on -- rather than the question. Case-insensitive whole-word matching, so
        "Iran" does not match "Iranian" only by accident of substring.
        """
        haystack = " ".join(
            fragment.lower()
            for fragment in (
                criteria.yes_condition or "",
                criteria.no_condition or "",
                *criteria.qualifying_events,
            )
        )
        words = set(_WORD.findall(haystack))
        return any(set(_WORD.findall(entity.lower())) <= words for entity in entities if entity)


def classify_kind(text: str) -> EventKind:
    """Classify a claim by keyword, longest phrase first.

    Longest-first is not a micro-optimisation: "ceasefire failed" contains "ceasefire",
    so a shortest-first scan classifies a collapse as an announcement -- an error that
    inverts the direction of the resulting probability shift.
    """
    lowered = text.lower()
    best: tuple[int, EventKind] | None = None
    for phrase, kind in KIND_KEYWORDS:
        if phrase in lowered and (best is None or len(phrase) > best[0]):
            best = (len(phrase), kind)
    return best[1] if best else EventKind.OTHER


def extract_entities(text: str, *, hints: tuple[str, ...] = ()) -> tuple[str, ...]:
    """Named actors, from the feed's hints first and the prose second.

    A capitalised-word scan is a weak extractor and is treated as such: stopwords are
    filtered, and a market match additionally requires the *resolution text* to name the
    same actor, so a spurious entity produces no match rather than a wrong one. Hints
    come first because a feed that resolved its own entities is better evidence than a
    regex over English.
    """
    found: list[str] = [hint.strip() for hint in hints if hint.strip()]
    for match in _CAPITALISED.finditer(text):
        candidate = match.group(1).strip()
        if candidate.split()[0] in _ENTITY_STOPWORDS:
            continue
        if candidate not in found:
            found.append(candidate)
    return tuple(found)


def claim_fingerprint(kind: EventKind, entities: tuple[str, ...]) -> str:
    """Identity of a *claim*, for counting independent reports of the same thing.

    Kind plus the sorted entity set. Deliberately coarse: two wires describing one strike
    will not share wording, and a fingerprint sensitive to phrasing would count them as
    two separate claims and never reach corroboration -- which is the failure mode that
    matters here, since the whole point is to require a second witness.
    """
    return f"{kind}:{','.join(sorted(entity.lower() for entity in entities))}"
