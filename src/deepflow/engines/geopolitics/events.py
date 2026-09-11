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
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from deepflow.core.types import ConditionId


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


class EventPipeline:
    """Ingests sources and emits validated events."""

    async def ingest(self, raw: object) -> GeopoliticalEvent | None:
        """Run one raw item through the pipeline.

        TODO(skeleton): validate the source, extract the claim, resolve
        entities, classify kind/direction/severity, compute weighted
        reliability, and require corroboration before emitting.
        """
        raise NotImplementedError("EventPipeline.ingest")

    async def map_to_markets(self, event: GeopoliticalEvent) -> tuple[ConditionId, ...]:
        """Find markets whose resolution criteria the event bears on.

        Matched against parsed resolution criteria, not market titles -- an
        event can be real, relevant, and still not satisfy the specific
        qualifying condition a market pays out on.
        """
        raise NotImplementedError("EventPipeline.map_to_markets")
