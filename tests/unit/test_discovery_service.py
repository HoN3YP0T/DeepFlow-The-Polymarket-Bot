"""Discovery lifecycle: what advances, what is refused, and what is recorded.

The rejection path gets as much attention as the happy path, because the rejected
set is the evidence for whether the gates are calibrated. A sweep that silently
drops a market it could not classify leaves no way to tell a protective gate from
one that is never satisfied.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from deepflow.config.thresholds import Thresholds
from deepflow.core.domain import Market, Outcome
from deepflow.core.enums import MarketCategory, OutcomeSide, ResolutionValidity
from deepflow.core.state_machine import MarketState
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.pipeline.classifier import MarketClassifier
from deepflow.pipeline.discovery import (
    KIND_MARKET_REJECTED,
    KIND_TRANSITION,
    DiscoveryService,
)
from deepflow.pipeline.resolution import ResolutionValidator

NOW = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)

#: A resolution text that parses to VALID. Markets default to it so a test about
#: classification is not silently also a test of resolution parsing.
VALID_TEXT = (
    'This market will resolve to "Yes" if the event occurs before December 31, '
    '2026, 11:59 PM ET. Otherwise, this market will resolve to "No". '
    "The resolution source for this market is the Associated Press."
)
AMBIGUOUS_TEXT = VALID_TEXT + " Settled by a consensus of credible reporting."


def _market(
    condition_id: str,
    *,
    question: str = "Will something happen?",
    tag_ids: tuple[str, ...] = (),
    sports_market_type: str | None = None,
    resolution_text: str | None = VALID_TEXT,
) -> Market:
    return Market(
        condition_id=ConditionId(condition_id),
        question=question,
        outcomes=(
            Outcome(token_id=ClobTokenId(f"{condition_id}-y"), label="Yes", side=OutcomeSide.YES),
            Outcome(token_id=ClobTokenId(f"{condition_id}-n"), label="No", side=OutcomeSide.NO),
        ),
        active=True,
        closed=False,
        accepting_orders=True,
        tag_ids=tag_ids,
        sports_market_type=sports_market_type,
        resolution_text=resolution_text,
        game_start_time=NOW + timedelta(hours=2) if sports_market_type else None,
        end_date=NOW + timedelta(days=30),
    )


class _FakeDiscovery:
    def __init__(self, markets: Sequence[Market]) -> None:
        self._markets = markets
        self.calls = 0

    async def list_active_markets(self, *, limit: int = 500) -> Sequence[Market]:
        self.calls += 1
        return self._markets[:limit]

    async def get_market(self, condition_id: ConditionId) -> Market | None:
        return next((m for m in self._markets if m.condition_id == condition_id), None)


class _FakeMarkets:
    def __init__(self) -> None:
        self.upserts: list[str] = []
        self.classifications: list[dict[str, Any]] = []

    async def upsert(self, market: Market) -> None:
        self.upserts.append(str(market.condition_id))

    async def record_classification(self, condition_id: ConditionId, **kw: Any) -> None:
        self.classifications.append({"condition_id": str(condition_id), **kw})

    async def get(self, condition_id: ConditionId) -> Market | None:
        return None

    async def list_tracked(self) -> Sequence[Market]:
        return ()


class _FakeJournal:
    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    async def record_decision(self, entry: dict[str, Any]) -> None:
        if not entry.get("kind") or not entry.get("reason"):
            raise ValueError("journal entry requires both 'kind' and 'reason'")
        self.entries.append(entry)

    async def record_signal(self, signal: Any) -> None: ...

    async def list_recent(self, *, limit: int = 100) -> Sequence[dict[str, Any]]:
        return tuple(reversed(self.entries))[:limit]


class _FakeUow:
    def __init__(self, markets: _FakeMarkets, journal: _FakeJournal) -> None:
        self.markets = markets
        self.journal = journal
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None: ...


def _service(
    markets: Sequence[Market], *, persist: bool = True
) -> tuple[DiscoveryService, _FakeMarkets, _FakeJournal]:
    market_repo, journal = _FakeMarkets(), _FakeJournal()
    unit = _FakeUow(market_repo, journal)

    @asynccontextmanager
    async def factory() -> AsyncIterator[Any]:
        yield unit

    service = DiscoveryService(
        discovery=_FakeDiscovery(markets),
        classifier=MarketClassifier(Thresholds()),
        validator=ResolutionValidator(),
        thresholds=Thresholds(),
        uow_factory=factory if persist else None,
    )
    return service, market_repo, journal


# --- Advancing ------------------------------------------------------------
async def test_classifiable_market_with_readable_rules_reaches_monitored() -> None:
    service, _, _ = _service([_market("0x1", tag_ids=("264",))])
    report = await service.run_once()

    assert (report.classified, report.monitored, report.rejected) == (1, 1, 0)
    assert service.lifecycle_for("0x1").state is MarketState.MONITORED


async def test_a_market_with_unreadable_rules_never_reaches_monitored() -> None:
    """The edge enforcing 'never trade from the title alone'. Classification alone
    does not open it -- the resolution rules have to parse."""
    service, _, _ = _service([_market("0x1", tag_ids=("264",), resolution_text=None)])
    await service.run_once()

    assert service.monitored_count == 0
    assert service.tradeable_tokens() == ()
    assert service.lifecycle_for("0x1").state is MarketState.MARKET_INVALID


async def test_category_tally_is_reported() -> None:
    service, _, _ = _service(
        [
            _market("0x1", tag_ids=("264",)),
            _market("0x2", tag_ids=("264",)),
            _market("0x3", tag_ids=("517",), sports_market_type="moneyline"),
        ]
    )
    report = await service.run_once()
    assert report.by_category[MarketCategory.POLITICS] == 2
    assert report.by_category[MarketCategory.CRICKET] == 1


# --- Rejection ------------------------------------------------------------
async def test_unclassifiable_market_is_rejected() -> None:
    service, _, journal = _service([_market("0x9", question="Will the widget ship?")])
    report = await service.run_once()

    assert report.rejected == 1
    assert service.lifecycle_for("0x9").state is MarketState.MARKET_INVALID
    rejections = [e for e in journal.entries if e["kind"] == KIND_MARKET_REJECTED]
    assert len(rejections) == 1
    assert "unclassified" in rejections[0]["reason"]


async def test_rejection_carries_enough_to_diagnose_it() -> None:
    """A rejection row recording only that something was refused cannot tell you
    which threshold to move."""
    service, _, journal = _service([_market("0x9", question="Will the widget ship?")])
    await service.run_once()
    entry = next(e for e in journal.entries if e["kind"] == KIND_MARKET_REJECTED)

    assert entry["condition_id"] == "0x9"
    assert entry["question"] == "Will the widget ship?"
    assert entry["reason"]


async def test_rejection_reasons_are_bucketed() -> None:
    """Bucketed on the leading clause: the full rationale is per-market and unique,
    which would make every count 1 and the tally useless."""
    service, _, _ = _service([_market(f"0x{i}", question="Unmatched question") for i in range(4)])
    report = await service.run_once()
    assert report.rejection_reasons == {"unclassified": 4}
    assert report.rejection_rate == 1.0


# --- Idempotence ----------------------------------------------------------
async def test_second_sweep_leaves_decided_markets_alone() -> None:
    """Re-deciding every sweep would churn the journal with identical rows and let a
    market rejected once become eligible again because the classifier's inputs
    wobbled."""
    markets = [_market("0x1", tag_ids=("264",)), _market("0x9", question="Widget?")]
    service, _, journal = _service(markets)

    first = await service.run_once()
    before = len(journal.entries)
    second = await service.run_once()

    assert (first.monitored, first.rejected) == (1, 1)
    assert (second.classified, second.rejected, second.unchanged) == (0, 0, 2)
    assert len(journal.entries) == before


# --- Persistence ----------------------------------------------------------
async def test_classification_is_persisted_with_its_state() -> None:
    service, market_repo, _ = _service([_market("0x1", tag_ids=("264",))])
    await service.run_once()

    assert market_repo.upserts == ["0x1"]
    written = market_repo.classifications[0]
    assert written["category"] == MarketCategory.POLITICS.value
    assert written["lifecycle_state"] == MarketState.CLASSIFIED.value
    assert written["confidence"] > Decimal("0.9")

    # The final write records where the market actually ended up.
    assert market_repo.classifications[-1]["lifecycle_state"] == MarketState.MONITORED.value


async def test_rejected_market_is_persisted_as_invalid() -> None:
    """The row must record the refusal. Left at DISCOVERED, the next sweep would
    re-decide it; left absent, nothing records that it was ever seen."""
    service, market_repo, _ = _service([_market("0x9", question="Widget?")])
    await service.run_once()

    written = market_repo.classifications[0]
    assert written["category"] == MarketCategory.UNKNOWN.value
    assert written["lifecycle_state"] == MarketState.MARKET_INVALID.value


async def test_transitions_are_journalled_with_both_endpoints() -> None:
    """Current state is on the market row; the path to it is only here, and the path
    is what you want when a market behaved oddly."""
    service, _, journal = _service([_market("0x1", tag_ids=("264",))])
    await service.run_once()

    entry = next(e for e in journal.entries if e["kind"] == KIND_TRANSITION)
    assert entry["source"] == MarketState.DISCOVERED.value
    assert entry["target"] == MarketState.CLASSIFIED.value
    assert entry["category"] == MarketCategory.POLITICS.value


async def test_runs_without_persistence() -> None:
    """The sweep must work with no database wired, so classification can be
    exercised in isolation."""
    service, _, _ = _service([_market("0x1", tag_ids=("264",))], persist=False)
    report = await service.run_once()
    assert report.classified == 1


# --- Robustness -----------------------------------------------------------
async def test_empty_catalogue_is_not_an_error() -> None:
    service, _, _ = _service([])
    report = await service.run_once()
    assert report.seen == 0
    assert report.rejection_rate == 0.0


async def test_limit_is_passed_through() -> None:
    service, _, _ = _service([_market(f"0x{i}", tag_ids=("264",)) for i in range(10)])
    report = await service.run_once(limit=3)
    assert report.seen == 3


# --- Resolution gating ----------------------------------------------------
def _with_text(condition_id: str, text: str | None) -> Market:
    return _market(condition_id, tag_ids=("264",), resolution_text=text)


async def test_valid_resolution_reaches_monitored() -> None:
    service, _, _ = _service([_with_text("0x1", VALID_TEXT)])
    report = await service.run_once()

    assert report.monitored == 1
    assert service.lifecycle_for("0x1").state is MarketState.MONITORED
    assert service.monitored_count == 1
    assert len(service.tradeable_tokens()) == 2


async def test_path_runs_through_validated() -> None:
    """Two edges, not one. VALIDATED records that the rules were read and accepted;
    MONITORED records that we are watching. Collapsing them loses the distinction a
    post-mortem needs."""
    service, _, _ = _service([_with_text("0x1", VALID_TEXT)])
    await service.run_once()
    states = [t.target for t in service.lifecycle_for("0x1").history]
    assert states == [MarketState.CLASSIFIED, MarketState.VALIDATED, MarketState.MONITORED]


async def test_ambiguous_resolution_is_rejected_not_monitored() -> None:
    """The guard against grading drifting into permission: AMBIGUOUS parses fine and
    still must not be traded."""
    service, _, journal = _service([_with_text("0x2", AMBIGUOUS_TEXT)])
    report = await service.run_once()

    assert report.monitored == 0
    assert report.rejected == 1
    assert service.tradeable_tokens() == ()
    entry = next(e for e in journal.entries if e["kind"] == KIND_MARKET_REJECTED)
    assert "AMBIGUOUS" in entry["reason"]


async def test_unreadable_resolution_is_rejected() -> None:
    service, _, _ = _service([_with_text("0x3", "Prose that never says how it settles.")])
    report = await service.run_once()
    assert report.rejected == 1
    assert service.lifecycle_for("0x3").state is MarketState.MARKET_INVALID


async def test_classified_but_unvalidated_never_becomes_tradeable() -> None:
    """A classified market whose payout condition nobody could read does not belong in
    the streaming set, however liquid it looks."""
    service, _, _ = _service(
        [_with_text("0x1", VALID_TEXT), _with_text("0x2", AMBIGUOUS_TEXT), _with_text("0x3", None)]
    )
    report = await service.run_once()

    assert report.classified == 3
    assert report.monitored == 1
    assert len(service.tradeable_tokens()) == 2  # only the valid market's two outcomes


async def test_validity_tally_is_reported() -> None:
    service, _, _ = _service([_with_text("0x1", VALID_TEXT), _with_text("0x2", AMBIGUOUS_TEXT)])
    report = await service.run_once()
    assert report.by_validity[ResolutionValidity.VALID] == 1
    assert report.by_validity[ResolutionValidity.AMBIGUOUS] == 1


async def test_resolution_validity_is_persisted() -> None:
    service, market_repo, _ = _service([_with_text("0x2", AMBIGUOUS_TEXT)])
    await service.run_once()
    assert market_repo.classifications[-1]["resolution_validity"] == (
        ResolutionValidity.AMBIGUOUS.value
    )


async def test_every_market_is_resolved_one_way_or_the_other() -> None:
    """Phase 2's done-when, as a test: nothing is left in limbo."""
    markets = [
        _with_text("0x1", VALID_TEXT),
        _with_text("0x2", AMBIGUOUS_TEXT),
        _with_text("0x3", None),
        _market("0x4", question="Unclassifiable widget question"),
    ]
    service, _, _ = _service(markets)
    report = await service.run_once()
    assert report.seen - report.monitored - report.rejected == 0
