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
from datetime import timedelta
from decimal import Decimal
from typing import Final

from deepflow.config.thresholds import GeopoliticsThresholds
from deepflow.core.clock import Clock, SystemClock
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

#: How long an operator-supplied prior stays usable.
#:
#: A week. A prior nobody has revisited in that time is not evidence, and a process left
#: running for a month would otherwise keep signalling on it. Refused rather than decayed
#: toward the market: there is no defensible rate at which a human judgement turns into a
#: different number by itself.
MAX_PRIOR_AGE = timedelta(days=7)

#: How far a prior may sit from the market before it is treated as an error, not an edge.
#:
#: Added after a verification run: a prior of 0.97 typed against a market trading at 0.18 was
#: approved by the full gate, reporting an edge of 0.79 and a net EV of 0.78 (§87). Every
#: safeguard behaved correctly and none of them could help, because nothing in the system can
#: distinguish a sourced prior from a fabricated one -- ``source`` is free text.
#:
#: At 25 points of divergence the likelier explanation is a stale, mistyped or misaligned
#: prior than a market that wrong, and the cost of the two errors is not symmetric: refusing
#: costs a trade, accepting sizes a position at odds nobody checked.
#:
#: **This reads the market price, and that is not a breach of the independence contract.** The
#: contract forbids *deriving* the estimate from the price, because an edge computed from its
#: own input is an artefact. Using the price as a plausibility bound on a number we supplied
#: can only ever suppress a trade, never create or enlarge one.
MAX_PRIOR_DIVERGENCE: Final = Decimal("0.25")


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
        clock: Clock | None = None,
    ) -> None:
        self._pipeline = pipeline
        self._thresholds = thresholds
        self._base_rates = dict(base_rates or {})
        self._events: dict[ConditionId, list[GeopoliticalEvent]] = {}
        self._clock = clock or SystemClock()

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
        """Probability from a sourced prior, optionally shifted by unpriced events.

        **Abstains without a prior**, which is the only hard requirement. These markets have
        no continuously observable state, so the alternatives to an external prior are the
        market price -- circular, and forbidden by this class's contract -- or a number
        invented here. A prior is supplied by an operator via ``Settings.base_rates`` and
        must name its source; nothing in this system can derive one.

        **Also abstains on a prior older than a week.** A judgement nobody has revisited is
        not evidence, and a long-running process would otherwise signal on it forever.

        With a prior and no events the estimate is the prior itself, carrying the prior's own
        (deliberately wide) uncertainty. With events, the prior moves toward or away from the
        outcome in proportion to the most severe event in each direction, capped, and the
        uncertainty widens by the size of that shift -- an event-driven adjustment is the
        least certain estimate this system makes, and the buffer is what stops it being sized
        as though it had been measured.
        """
        base_rate = self._base_rates.get(market.condition_id)
        if base_rate is None:
            return None

        age = self._clock.now() - base_rate.as_of
        if age > MAX_PRIOR_AGE:
            # A prior nobody has revisited in a week is not evidence, and a long-running
            # process would otherwise keep signalling on it indefinitely. Refused rather than
            # decayed: there is no defensible rate at which a human judgement becomes a
            # different number on its own.
            log.info(
                "event_driven.prior_stale",
                condition_id=str(market.condition_id),
                age_hours=round(age.total_seconds() / 3600, 1),
                source=base_rate.source,
            )
            return None

        divergence = self._divergence(base_rate, snapshot, token_id)
        if divergence is not None and divergence > MAX_PRIOR_DIVERGENCE:
            log.warning(
                "event_driven.prior_implausible",
                condition_id=str(market.condition_id),
                prior=str(base_rate.probability),
                divergence=str(divergence),
                source=base_rate.source,
            )
            return None

        events = tuple(
            event for event in self._events.get(market.condition_id, ()) if event.is_actionable
        )
        # **A sourced prior is itself evidence, and on its own is enough to have an opinion.**
        #
        # This engine originally required a corroborated event before it would speak, on the
        # grounds that without unpriced evidence the market is probably right. That reasoning
        # holds for an engine with no information and not for one an operator has handed a
        # sourced prior: "polling says 0.97 and the market is 0.90" is exactly the edge this
        # system looks for, and demanding a news event on top made the prior unusable — which
        # mattered, because no news feed exists and politics is 98 of the 100 markets a sweep
        # returns.
        #
        # What keeps it honest is that the prior arrives wide (0.15 by default, deliberately
        # wider than any model output here), carries its source into the journal, and still
        # faces the EV uncertainty buffer, the Kelly haircut, the 17-check gate and the risk
        # engine. Events remain a modifier rather than a precondition.
        shift = self._shift(events) if events else Decimal(0)

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
                # Default 0 rather than omitted: with no events the severity genuinely is
                # zero, and a missing key would make a prior-only estimate look like a row
                # someone forgot to fill in.
                "max_severity": str(max((event.severity for event in events), default=0)),
            },
        )

    @staticmethod
    def _divergence(
        base_rate: BaseRate, snapshot: MarketSnapshot, token_id: ClobTokenId
    ) -> Decimal | None:
        """How far the prior sits from what the market is charging, or ``None`` if unreadable.

        ``None`` when there is no ask: an unreadable book cannot bound anything, and treating
        "no price" as "no divergence" would let the check pass exactly when it has nothing to
        check. The caller then proceeds on the other guards, since a missing ask already stops
        the trade downstream -- there is nothing to buy.
        """
        book = snapshot.book_for(token_id)
        price = book.best_ask if book is not None else None
        if price is None or price <= 0:
            return None
        return abs(base_rate.probability - price)

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
