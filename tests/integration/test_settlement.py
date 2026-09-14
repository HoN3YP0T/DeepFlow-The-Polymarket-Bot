"""The settlement loop, end to end against a real database.

This is the half of calibration that did not exist. Until it did, ``signals`` held a
model probability and nothing anywhere held the outcome, so every (prediction,
outcome) pair was half missing and no amount of running could produce a fit.

The venue is faked here and the database is real, which is the right way round: the
venue call is one method whose shape is pinned in
``tests/unit/test_resolutions_adapter.py``, while the queue query, the writes and the
scoring join are SQL, and SQL is what a fake cannot tell you about.
"""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from deepflow.adapters.persistence.repositories import SqlUnitOfWork
from deepflow.core.domain import (
    Market,
    MarketResolution,
    Outcome,
    OutcomePayout,
    Prediction,
)
from deepflow.core.enums import MarketCategory, OutcomeSide
from deepflow.pipeline.settlement import SettlementRecorder

NOW = datetime.now(UTC).replace(microsecond=0)
YES = "tok-yes"
NO = "tok-no"


def _sessions(session: AsyncSession) -> Any:
    """A session factory over the test's own session.

    The session is not closed on exit: the conftest owns its lifetime, and closing it
    here would take the outer transaction with it.
    """

    @asynccontextmanager
    async def factory() -> Any:
        yield session

    return factory


class _Venue:
    """Stands in for ``PolymarketResolutions``, recording what it was asked."""

    def __init__(self, *, settled: set[str] | None = None) -> None:
        self.settled = settled if settled is not None else set()
        self.asked: list[tuple[str, ...]] = []

    async def resolve(self, markets: Sequence[Market]) -> tuple[MarketResolution, ...]:
        self.asked.append(tuple(str(m.condition_id) for m in markets))
        return tuple(
            MarketResolution(
                condition_id=market.condition_id,
                payouts=(
                    OutcomePayout(token_id=YES, payout=Decimal(1)),  # type: ignore[arg-type]
                    OutcomePayout(token_id=NO, payout=Decimal(0)),  # type: ignore[arg-type]
                ),
                resolved_at=NOW,
                status="resolved",
                source="reported",
            )
            for market in markets
            if str(market.condition_id) in self.settled
        )


def _market(condition_id: str, *, ended: bool = True) -> Market:
    return Market(
        condition_id=condition_id,  # type: ignore[arg-type]
        question="Will it go up?",
        slug=f"slug-{condition_id}",
        outcomes=(
            Outcome(token_id=YES, label="Up", side=OutcomeSide.YES),  # type: ignore[arg-type]
            Outcome(token_id=NO, label="Down", side=OutcomeSide.NO),  # type: ignore[arg-type]
        ),
        active=not ended,
        closed=ended,
        accepting_orders=not ended,
        end_date=NOW - timedelta(hours=1) if ended else NOW + timedelta(hours=1),
    )


def _prediction(condition_id: str, probability: str = "0.9") -> Prediction:
    return Prediction(
        engine="btc_5m",
        category=MarketCategory.BTC_5M,
        condition_id=condition_id,  # type: ignore[arg-type]
        token_id=YES,  # type: ignore[arg-type]
        model_probability=Decimal(probability),
        calibrated_probability=Decimal(probability),
        uncertainty=Decimal("0.1"),
        predicted_at=NOW - timedelta(minutes=5),
        horizon_seconds=120,
    )


async def _seed(uow: SqlUnitOfWork, condition_id: str, *, ended: bool = True) -> None:
    await uow.markets.upsert(_market(condition_id, ended=ended))
    await uow.predictions.record(_prediction(condition_id))
    await uow.commit()


async def test_a_pass_records_outcomes_and_makes_predictions_scoreable(
    session: AsyncSession, uow: SqlUnitOfWork
) -> None:
    """The whole point: after a pass, a prediction has an outcome beside it."""
    await _seed(uow, "0xaa")
    assert await uow.predictions.list_samples() == ()

    venue = _Venue(settled={"0xaa"})
    recorder = SettlementRecorder(_sessions(session), venue)  # type: ignore[arg-type]
    assert await recorder.run_once() == 1

    samples = await uow.predictions.list_samples()
    assert len(samples) == 1
    assert samples[0].predicted == Decimal("0.9")
    assert samples[0].realized == Decimal(1)


async def test_a_second_pass_does_not_re_ask_about_a_settled_market(
    session: AsyncSession, uow: SqlUnitOfWork
) -> None:
    """A queue that never drains re-spends a 20-id-per-request budget on the same
    markets, and the backlog outruns the loop."""
    await _seed(uow, "0xaa")
    venue = _Venue(settled={"0xaa"})
    recorder = SettlementRecorder(_sessions(session), venue)  # type: ignore[arg-type]

    await recorder.run_once()
    assert await recorder.run_once() == 0
    assert venue.asked == [("0xaa",)]


async def test_a_market_that_has_not_ended_is_never_asked_about(
    session: AsyncSession, uow: SqlUnitOfWork
) -> None:
    await _seed(uow, "0xopen", ended=False)
    venue = _Venue()
    recorder = SettlementRecorder(_sessions(session), venue)  # type: ignore[arg-type]

    assert await recorder.run_once() == 0
    assert venue.asked == []


async def test_a_market_the_venue_has_not_settled_yet_stays_queued(
    session: AsyncSession, uow: SqlUnitOfWork
) -> None:
    """Ended is not settled. UMA proposal, challenge and review take time, and a
    market under review must come back on the next pass rather than being written off.
    """
    await _seed(uow, "0xaa")
    venue = _Venue(settled=set())
    recorder = SettlementRecorder(_sessions(session), venue)  # type: ignore[arg-type]

    assert await recorder.run_once() == 0
    assert recorder.pending == 1
    assert await recorder.run_once() == 0
    assert venue.asked == [("0xaa",), ("0xaa",)]


async def test_pending_reports_the_backlog(session: AsyncSession, uow: SqlUnitOfWork) -> None:
    """Surfaced on the health line: `settled` stuck at zero while predictions climb is
    the shape that means scoring is broken rather than merely waiting."""
    for index in range(3):
        await _seed(uow, f"0x{index:02x}")
    recorder = SettlementRecorder(_sessions(session), _Venue())  # type: ignore[arg-type]

    await recorder.run_once()
    assert recorder.pending == 3


async def test_a_market_never_persisted_is_skipped_rather_than_guessed(
    session: AsyncSession, uow: SqlUnitOfWork
) -> None:
    """The payout pair is positional, so without the market's own outcome order there
    is nothing to align it to. Asking anyway would spend budget for a row that could
    only be recorded by guessing."""
    await uow.predictions.record(_prediction("0xghost"))
    await uow.commit()

    venue = _Venue(settled={"0xghost"})
    recorder = SettlementRecorder(_sessions(session), venue)  # type: ignore[arg-type]
    assert await recorder.run_once() == 0
    # The market row is missing, so it is not even in the queue, let alone asked about.
    assert venue.asked == []


async def test_counters_accumulate_across_passes(session: AsyncSession, uow: SqlUnitOfWork) -> None:
    await _seed(uow, "0xaa")
    await _seed(uow, "0xbb")
    recorder = SettlementRecorder(_sessions(session), _Venue(settled={"0xaa", "0xbb"}))  # type: ignore[arg-type]

    await recorder.run_once()
    assert recorder.recorded == 2
