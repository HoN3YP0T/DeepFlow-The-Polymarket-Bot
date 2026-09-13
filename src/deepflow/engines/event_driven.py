"""Event-driven probability, shared by the geopolitical and political engines.

These markets have **no continuously observable state.** A football match has a score and
a clock; "will X resign by June" has neither, and its true probability sits still between
announcements. That single fact shapes everything here:

* An engine of this kind needs a prior from outside itself, because the only other
  candidates are the market price -- circular, and forbidden by
  :class:`BaseProbabilityEngine`'s contract -- or a number invented on the spot.
* It has an opinion only when it holds evidence the market has not priced. The rest of
  the time the honest answer is "the market is probably right", which is an abstention.

Extracted rather than inherited one from the other: a political market is not a kind of
geopolitical market, and making one a subclass of the other would encode a relationship
that does not exist. What they genuinely share is this mechanism, and it lives in one
place so the next correction to it lands once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from deepflow.config.thresholds import GeopoliticsThresholds
from deepflow.core.domain import BaseRate, Market, MarketSnapshot, ProbabilityEstimate
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.engines.base import BaseProbabilityEngine
from deepflow.engines.geopolitics.events import (
    Direction,
    EventPipeline,
    GeopoliticalEvent,
)

log = get_logger(__name__)

#: Maximum probability move a full-severity event may produce.
#:
#: The edge here is that the market has not yet priced an event, not that it has
#: mispriced one by a distance. A keyword-classified news item is not evidence strong
#: enough to justify more, and a tight cap keeps a misclassification cheap.
MAX_EVENT_SHIFT: Final = Decimal("0.15")


def _clamp(value: Decimal) -> Decimal:
    """Keep a probability inside [0, 1] without silently inverting it."""
    return min(Decimal(1), max(Decimal(0), value))


class EventDrivenEngine(BaseProbabilityEngine):
    """A sourced prior, moved by corroborated events the market has not priced.

    Subclasses declare their categories and may narrow ``_shift``; the reasoning is here.
    """

    def __init__(
        self,
        pipeline: EventPipeline,
        thresholds: GeopoliticsThresholds,
        *,
        base_rates: Mapping[ConditionId, BaseRate] | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._thresholds = thresholds
        self._base_rates = dict(base_rates or {})
        self._events: dict[ConditionId, list[GeopoliticalEvent]] = {}

    def observe(self, event: GeopoliticalEvent, markets: Sequence[ConditionId]) -> None:
        """Hold an actionable event against the markets it bears on.

        Non-actionable events are **not** held. An uncorroborated report is real
        information for a human reading the dashboard, and it is not a basis for moving a
        probability -- keeping it here would let it accumulate into an estimate nobody
        decided to trust.
        """
        if not event.is_actionable:
            log.info(
                "geopolitics.event_not_actionable",
                kind=str(event.kind),
                sources=event.corroborating_source_count,
                reliability=str(event.reliability),
            )
            return
        for condition_id in markets:
            self._events.setdefault(condition_id, []).append(event)

    def set_base_rate(self, condition_id: ConditionId, base_rate: BaseRate) -> None:
        """Supply the prior for one market. Without it this engine cannot estimate."""
        self._base_rates[condition_id] = base_rate

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """Probability from a sourced prior, shifted by actionable unpriced events.

        **Abstains in two cases, and both are the common one.**

        *No base rate* -- there is nothing to anchor on. These markets have no
        continuously observable state, so the only alternatives to an external prior are
        the market price, which is circular and forbidden by this class's contract, or a
        number invented here. Nothing in this system constructs a base rate today, so in
        practice this branch is always taken (see ``docs/STATUS.md``): the engine is
        machinery waiting on an input, and saying so is better than shipping a fabricated
        prior that sizing would believe.

        *No actionable event* -- the market is probably right. With no unpriced evidence
        there is no edge, and an engine that produces a number here is manufacturing one.

        With both, the estimate is the prior moved toward or away from the outcome in
        proportion to the event's severity, capped, and carrying the prior's uncertainty
        **widened** by the shift: an event-driven adjustment is the least certain kind of
        estimate this system makes, and the uncertainty buffer is what stops it being
        sized as though it were a measurement.
        """
        base_rate = self._base_rates.get(market.condition_id)
        if base_rate is None:
            return None

        events = tuple(
            event for event in self._events.get(market.condition_id, ()) if event.is_actionable
        )
        if not events:
            return None

        shift = self._shift(events)
        if shift == 0:
            # Events are held but none of them has a direction -- a negotiation, an
            # ambassadorial statement. Recorded, and deliberately not traded on: an
            # event with no sign is not evidence for either side.
            return None

        raw = _clamp(base_rate.probability + shift)
        return ProbabilityEstimate(
            token_id=token_id,
            model_probability=raw,
            calibrated_probability=self._calibrate(raw),
            uncertainty=_clamp(base_rate.uncertainty + abs(shift)),
            engine=self.name,
            inputs={
                "base_rate": str(base_rate.probability),
                "base_rate_source": base_rate.source,
                "base_rate_as_of": base_rate.as_of.isoformat(),
                "shift": str(shift),
                "events": str(len(events)),
                "kinds": ",".join(sorted({str(event.kind) for event in events})),
                "max_severity": str(max(event.severity for event in events)),
            },
        )

    def _shift(self, events: Sequence[GeopoliticalEvent]) -> Decimal:
        """Net probability shift from the events held, capped.

        Driven by the **most severe** event in each direction rather than the sum. Ten
        reports of one escalation are one escalation, and summing them would let coverage
        volume masquerade as evidence -- the same error the corroboration count exists to
        avoid, arriving by another route.

        The cap is deliberately tight. This engine's edge is that the market has not yet
        priced an event, not that it has mispriced one by a distance, and a large shift
        from a keyword-classified news item is not a claim this system can support.
        """
        escalation = max(
            (e.severity for e in events if e.direction is Direction.ESCALATION), default=0
        )
        de_escalation = max(
            (e.severity for e in events if e.direction is Direction.DE_ESCALATION), default=0
        )
        net = Decimal(escalation - de_escalation) / Decimal(100)
        return net * MAX_EVENT_SHIFT
