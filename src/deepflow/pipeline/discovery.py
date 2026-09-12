"""Discovery loop. Section 3.

Polls for active markets, drives each one from DISCOVERED through CLASSIFIED to
VALIDATED, and hands the survivors to the monitoring set.

Runs on a slow cadence deliberately: the market catalogue changes on the order
of minutes, and re-validating resolution rules on every tick wastes calls that
the trading path needs.

**Current reach: CLASSIFIED.** The state machine has no ``CLASSIFIED -> MONITORED``
edge -- that is the edge enforcing *never trade from the title alone* -- so nothing
advances past classification until :class:`ResolutionValidator` is implemented. No
bypass is added for the interim. A temporary hole in a safety interlock has a way of
outliving the reason it was made, and the rejection corpus this sweep builds is
exactly what the validator should be tested against.

Every rejection and every transition is journalled. The rejected set is the
evidence for whether the gates are calibrated: the markets traded are a biased
sample of those seen, and without the refusals there is no way to tell a gate that
is correctly protective from one that is never satisfied.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field

from deepflow.config.thresholds import Thresholds
from deepflow.core.domain import Classification, Market
from deepflow.core.enums import MarketCategory, ResolutionValidity, RunMode
from deepflow.core.errors import IllegalTransitionError
from deepflow.core.logging import get_logger
from deepflow.core.state_machine import MarketLifecycle, MarketState
from deepflow.pipeline.classifier import MarketClassifier
from deepflow.pipeline.resolution import ResolutionValidator
from deepflow.ports.market_data import MarketDiscoveryPort
from deepflow.ports.repository import UnitOfWork

#: Builds a transaction scope. An async context manager per call rather than a
#: shared session: a sweep writes many independent decisions, and one failed
#: market must not roll back the verdicts already recorded for the others.
UnitOfWorkFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]

log = get_logger(__name__)

#: Journal ``kind`` values this service writes. Distinct from
#: :class:`~deepflow.journal.recorder.JournalKind`, which covers trade decisions --
#: these are pipeline events, and mixing them would make "how many rejections did
#: the safety gate produce?" unanswerable.
KIND_TRANSITION = "TRANSITION"
KIND_MARKET_REJECTED = "MARKET_REJECTED"


@dataclass(slots=True)
class SweepReport:
    """What one sweep did. Returned rather than logged so the caller can assert."""

    seen: int = 0
    classified: int = 0
    rejected: int = 0
    unchanged: int = 0
    by_category: dict[MarketCategory, int] = field(default_factory=dict)
    rejection_reasons: dict[str, int] = field(default_factory=dict)

    @property
    def rejection_rate(self) -> float:
        return self.rejected / self.seen if self.seen else 0.0


class DiscoveryService:
    """Finds, classifies and validates markets."""

    def __init__(
        self,
        *,
        discovery: MarketDiscoveryPort,
        classifier: MarketClassifier,
        validator: ResolutionValidator,
        thresholds: Thresholds,
        uow_factory: UnitOfWorkFactory | None = None,
        mode: RunMode = RunMode.PAPER,
    ) -> None:
        self._discovery = discovery
        self._classifier = classifier
        self._validator = validator
        self._thresholds = thresholds
        self._uow_factory = uow_factory
        self._mode = mode
        self._lifecycles: dict[str, MarketLifecycle] = {}
        self._classifications: dict[str, Classification] = {}

    async def run_once(self, *, limit: int = 500) -> SweepReport:
        """One discovery sweep.

        Per market: ``DISCOVERED -> classify -> CLASSIFIED``, with an ``UNKNOWN``
        category going to ``MARKET_INVALID``. Validation and the step to
        ``MONITORED`` land with the validator.

        A market already past ``DISCOVERED`` is left alone. Re-classifying every
        sweep would churn the journal with identical rows and, worse, would let a
        market that was rejected once quietly become eligible again because the
        classifier's inputs wobbled.
        """
        report = SweepReport()
        markets = await self._discovery.list_active_markets(limit=limit)

        for market in markets:
            report.seen += 1
            key = str(market.condition_id)
            lifecycle = self.lifecycle_for(key)

            if lifecycle.state is not MarketState.DISCOVERED:
                report.unchanged += 1
                continue

            classification = self._classifier.classify(market)
            self._classifications[key] = classification

            if classification.category is MarketCategory.UNKNOWN:
                await self._reject(
                    market,
                    lifecycle,
                    reason=f"unclassified: {classification.rationale}",
                    report=report,
                )
                continue

            await self._advance(market, lifecycle, classification, report)

        log.info(
            "discovery.sweep",
            seen=report.seen,
            classified=report.classified,
            rejected=report.rejected,
            unchanged=report.unchanged,
            rejection_rate=round(report.rejection_rate, 3),
        )
        return report

    # --- Transitions ------------------------------------------------------
    async def _advance(
        self,
        market: Market,
        lifecycle: MarketLifecycle,
        classification: Classification,
        report: SweepReport,
    ) -> None:
        reason = f"{classification.category.value} @ {classification.confidence}"
        if not self._move(lifecycle, MarketState.CLASSIFIED, reason, market):
            return

        report.classified += 1
        report.by_category[classification.category] = (
            report.by_category.get(classification.category, 0) + 1
        )
        await self._persist(market, lifecycle, classification)
        await self._journal(
            kind=KIND_TRANSITION,
            reason=reason,
            condition_id=str(market.condition_id),
            source=MarketState.DISCOVERED.value,
            target=MarketState.CLASSIFIED.value,
            category=classification.category.value,
            confidence=classification.confidence,
            signals=list(classification.matched_signals),
        )

    async def _reject(
        self,
        market: Market,
        lifecycle: MarketLifecycle,
        *,
        reason: str,
        report: SweepReport,
    ) -> None:
        if not self._move(lifecycle, MarketState.MARKET_INVALID, reason, market):
            return

        report.rejected += 1
        # Bucket on the leading clause so the tally stays readable: the full
        # rationale is per-market and unique, which would make every count 1.
        bucket = reason.split(":")[0]
        report.rejection_reasons[bucket] = report.rejection_reasons.get(bucket, 0) + 1

        classification = self._classifications.get(str(market.condition_id))
        await self._persist(market, lifecycle, classification)
        await self._journal(
            kind=KIND_MARKET_REJECTED,
            reason=reason,
            condition_id=str(market.condition_id),
            question=market.question,
            tags=list(market.tags),
            source=MarketState.DISCOVERED.value,
            target=MarketState.MARKET_INVALID.value,
        )

    def _move(
        self, lifecycle: MarketLifecycle, target: MarketState, reason: str, market: Market
    ) -> bool:
        """Guarded transition. Returns whether it happened.

        An illegal edge is a wiring bug, not a market problem, so it is logged
        loudly and the sweep continues. Letting it propagate would abandon the
        remaining markets over one bad row.
        """
        try:
            lifecycle.transition(target, reason)
        except IllegalTransitionError:
            log.error(
                "discovery.illegal_transition",
                condition_id=str(market.condition_id),
                source=lifecycle.state.value,
                target=target.value,
            )
            return False
        return True

    # --- Persistence ------------------------------------------------------
    async def _persist(
        self,
        market: Market,
        lifecycle: MarketLifecycle,
        classification: Classification | None,
    ) -> None:
        """Write the market and its verdict.

        Both in one transaction: a market row whose lifecycle state disagrees with
        its classification is worse than neither, because the next sweep would trust
        the state and skip re-deciding.
        """
        if self._uow_factory is None:
            return
        async with self._uow_factory() as unit:
            await unit.markets.upsert(market)
            await unit.markets.record_classification(
                market.condition_id,
                category=(
                    classification.category if classification else MarketCategory.UNKNOWN
                ).value,
                confidence=classification.confidence if classification else None,
                lifecycle_state=lifecycle.state.value,
                resolution_validity=ResolutionValidity.NOT_CHECKED.value,
            )
            await unit.commit()

    async def _journal(self, *, kind: str, reason: str, **context: object) -> None:
        if self._uow_factory is None:
            return
        async with self._uow_factory() as unit:
            await unit.journal.record_decision(
                {"kind": kind, "reason": reason, "run_mode": self._mode.value, **context}
            )
            await unit.commit()

    # --- Inspection -------------------------------------------------------
    def lifecycle_for(self, condition_id: str) -> MarketLifecycle:
        """Lifecycle holder for a market, created on first sight."""
        return self._lifecycles.setdefault(condition_id, MarketLifecycle())

    def classification_for(self, condition_id: str) -> Classification | None:
        return self._classifications.get(condition_id)

    def tradeable_tokens(self) -> tuple[str, ...]:
        """Token ids of markets that reached the monitoring set.

        Empty until the validator exists, because ``MONITORED`` is unreachable. That
        is the honest answer rather than a convenient one: returning classified
        markets here would hand the streaming set markets whose resolution rules
        nobody has read.
        """
        return ()

    @property
    def monitored_count(self) -> int:
        """Markets actually in the monitoring set.

        Counts ``MONITORED`` specifically, not "everything not rejected". The
        broader reading reports 393 markets monitored while the validator is
        unimplemented and none have passed it -- a number that reads as healthy
        precisely when nothing is happening.
        """
        return sum(1 for lc in self._lifecycles.values() if lc.state is MarketState.MONITORED)

    @property
    def classified_count(self) -> int:
        """Past classification, not yet validated. Where everything sits today."""
        return sum(1 for lc in self._lifecycles.values() if lc.state is MarketState.CLASSIFIED)

    @property
    def rejected_count(self) -> int:
        return sum(1 for lc in self._lifecycles.values() if lc.state is MarketState.MARKET_INVALID)
