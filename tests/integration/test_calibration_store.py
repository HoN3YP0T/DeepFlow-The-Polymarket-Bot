"""Predictions, resolutions and the join that scores them, against real PostgreSQL.

The join is the part worth a real database. It is an inner join on
``(condition_id, token_id)``, and both halves of that are load-bearing: a left join
would score open markets as losses, and joining on the market alone would score
every prediction against whichever of a binary market's two outcome rows the planner
returned first. Neither mistake raises -- both just produce a confident, wrong curve.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.adapters.persistence.repositories import SqlUnitOfWork
from deepflow.core.domain import (
    Market,
    MarketResolution,
    Outcome,
    OutcomePayout,
    Prediction,
)
from deepflow.core.enums import MarketCategory, OutcomeSide

#: Anchored to the real clock, not a fixed literal.
#:
#: ``unresolved_condition_ids`` asks which markets have *ended*, which it answers
#: against ``SystemClock``. A fixed timestamp makes that test's meaning depend on the
#: date it runs: written as 2026-09-14T12:00 it passed in the afternoon and failed in
#: the morning, because "ended an hour ago" was still an hour in the future.
NOW = datetime.now(UTC).replace(microsecond=0)
YES = "tok-yes"
NO = "tok-no"


def _market(condition_id: str = "0xaa", *, end_date: datetime | None = None) -> Market:
    return Market(
        condition_id=condition_id,  # type: ignore[arg-type]
        question="Will it go up?",
        slug=f"slug-{condition_id}",
        outcomes=(
            Outcome(token_id=YES, label="Up", side=OutcomeSide.YES),  # type: ignore[arg-type]
            Outcome(token_id=NO, label="Down", side=OutcomeSide.NO),  # type: ignore[arg-type]
        ),
        active=False,
        closed=True,
        accepting_orders=False,
        end_date=end_date,
    )


def _prediction(
    probability: str,
    *,
    token_id: str = YES,
    condition_id: str = "0xaa",
    engine: str = "btc_5m",
    at: datetime = NOW,
) -> Prediction:
    return Prediction(
        engine=engine,
        category=MarketCategory.BTC_5M,
        condition_id=condition_id,  # type: ignore[arg-type]
        token_id=token_id,  # type: ignore[arg-type]
        model_probability=Decimal(probability),
        calibrated_probability=Decimal(probability),
        uncertainty=Decimal("0.1"),
        predicted_at=at,
        horizon_seconds=120,
    )


def _resolution(condition_id: str = "0xaa", *, yes_won: bool = True) -> MarketResolution:
    return MarketResolution(
        condition_id=condition_id,  # type: ignore[arg-type]
        payouts=(
            OutcomePayout(token_id=YES, payout=Decimal(1 if yes_won else 0)),  # type: ignore[arg-type]
            OutcomePayout(token_id=NO, payout=Decimal(0 if yes_won else 1)),  # type: ignore[arg-type]
        ),
        resolved_at=NOW + timedelta(minutes=5),
        status="resolved",
        source="reported",
    )


async def test_a_prediction_is_scored_once_its_market_settles(uow: SqlUnitOfWork) -> None:
    await uow.predictions.record(_prediction("0.9"))
    await uow.resolutions.upsert(_resolution(yes_won=True))
    await uow.commit()

    samples = await uow.predictions.list_samples()
    assert len(samples) == 1
    assert samples[0].predicted == Decimal("0.9")
    assert samples[0].realized == Decimal(1)
    assert samples[0].horizon_seconds == 120


async def test_each_token_is_scored_against_its_own_payout(uow: SqlUnitOfWork) -> None:
    """**The join bug that would not raise.**

    A binary market resolves both tokens, one to 1 and one to 0. Joining on the
    market alone would score both predictions against the same row -- and which row
    is a matter of planner mood, so the curve would be right or exactly inverted
    depending on the day.
    """
    await uow.predictions.record(_prediction("0.9", token_id=YES))
    await uow.predictions.record(_prediction("0.1", token_id=NO))
    await uow.resolutions.upsert(_resolution(yes_won=True))
    await uow.commit()

    scored = {s.token_id: s.realized for s in await uow.predictions.list_samples()}
    assert scored == {YES: Decimal(1), NO: Decimal(0)}


async def test_an_unsettled_market_contributes_nothing(uow: SqlUnitOfWork) -> None:
    """Not a zero. A left join filled with zero would read as "predicted 0.9, outcome
    0" for every open position -- and it would do it hardest to the engines whose
    markets run longest."""
    await uow.predictions.record(_prediction("0.9", condition_id="0xopen"))
    await uow.commit()
    assert await uow.predictions.list_samples() == ()


async def test_predictions_accumulate_rather_than_overwrite(uow: SqlUnitOfWork) -> None:
    """Append-only. An upsert here would mean the latest estimate on a market quietly
    erased the earlier ones, destroying the horizon variation a fit needs."""
    for minute, probability in enumerate(("0.5", "0.7", "0.95")):
        await uow.predictions.record(_prediction(probability, at=NOW + timedelta(minutes=minute)))
    await uow.resolutions.upsert(_resolution())
    await uow.commit()

    samples = await uow.predictions.list_samples()
    assert sorted(str(s.predicted) for s in samples) == ["0.50000000", "0.70000000", "0.95000000"]


async def test_a_reproposed_resolution_overwrites_the_old_outcome(uow: SqlUnitOfWork) -> None:
    """A disputed market can settle the other way. The venue's current answer is the
    one that paid, so the row moves rather than accumulating two truths."""
    await uow.predictions.record(_prediction("0.9"))
    await uow.resolutions.upsert(_resolution(yes_won=True))
    await uow.commit()
    assert (await uow.predictions.list_samples())[0].realized == Decimal(1)

    await uow.resolutions.upsert(_resolution(yes_won=False))
    await uow.commit()
    samples = await uow.predictions.list_samples()
    assert len(samples) == 1
    assert samples[0].realized == Decimal(0)


async def test_samples_can_be_narrowed_to_one_engine(uow: SqlUnitOfWork) -> None:
    """Curves are per engine: pooling a barrier model with an event-driven one fits a
    correction for neither."""
    await uow.predictions.record(_prediction("0.9", engine="btc_5m"))
    await uow.predictions.record(_prediction("0.8", engine="political", token_id=NO))
    await uow.resolutions.upsert(_resolution())
    await uow.commit()

    assert {s.engine for s in await uow.predictions.list_samples(engine="btc_5m")} == {"btc_5m"}
    assert len(await uow.predictions.list_samples()) == 2


async def test_only_ended_markets_we_predicted_on_are_queued_for_settlement(
    uow: SqlUnitOfWork,
) -> None:
    """The venue takes 20 condition ids per request. A sweep over everything closed
    would spend the whole budget on markets no sample will ever reference.
    """
    ended = _market("0xended", end_date=NOW - timedelta(hours=1))
    open_market = _market("0xopen", end_date=NOW + timedelta(hours=1))
    unpredicted = _market("0xquiet", end_date=NOW - timedelta(hours=1))
    for market in (ended, open_market, unpredicted):
        await uow.markets.upsert(market)
    await uow.predictions.record(_prediction("0.9", condition_id="0xended"))
    await uow.predictions.record(_prediction("0.9", condition_id="0xopen"))
    await uow.commit()

    queued = await uow.resolutions.unresolved_condition_ids()
    assert list(queued) == ["0xended"]


async def test_a_settled_market_leaves_the_queue(uow: SqlUnitOfWork) -> None:
    """Otherwise every pass re-asks about the same markets and the backlog never
    drains."""
    await uow.markets.upsert(_market("0xaa", end_date=NOW - timedelta(hours=1)))
    await uow.predictions.record(_prediction("0.9"))
    await uow.commit()
    assert list(await uow.resolutions.unresolved_condition_ids()) == ["0xaa"]

    await uow.resolutions.upsert(_resolution())
    await uow.commit()
    assert await uow.resolutions.unresolved_condition_ids() == ()


async def test_an_active_fit_is_stored_and_read_back(uow: SqlUnitOfWork) -> None:
    await uow.calibration.save(
        engine="btc_5m",
        knots={"engine": "btc_5m", "knots": [{"predicted": "0.9", "calibrated": "0.7"}]},
        samples=300,
        markets=80,
        brier_before=Decimal("0.21"),
        brier_after=Decimal("0.19"),
        ece_before=Decimal("0.08"),
        ece_after=Decimal("0.01"),
        activate=True,
    )
    await uow.commit()

    fits = await uow.calibration.active_fits()
    assert fits["btc_5m"]["knots"][0]["calibrated"] == "0.7"


async def test_activating_a_fit_retires_the_previous_one(uow: SqlUnitOfWork) -> None:
    """Exactly one curve per engine can be live, or "which curve sized this trade"
    has no answer."""
    for calibrated in ("0.70", "0.75"):
        await uow.calibration.save(
            engine="btc_5m",
            knots={"engine": "btc_5m", "knots": [{"predicted": "0.9", "calibrated": calibrated}]},
            samples=300,
            markets=80,
            brier_before=Decimal("0.21"),
            brier_after=Decimal("0.19"),
            ece_before=Decimal("0.08"),
            ece_after=Decimal("0.01"),
            activate=True,
        )
    await uow.commit()

    fits = await uow.calibration.active_fits()
    assert len(fits) == 1
    assert fits["btc_5m"]["knots"][0]["calibrated"] == "0.75"


async def test_a_stored_fit_without_activation_never_goes_live(uow: SqlUnitOfWork) -> None:
    """Fitting is a measurement; activating changes every probability the engine
    produces. The second must not be a side effect of the first."""
    await uow.calibration.save(
        engine="btc_5m",
        knots={"engine": "btc_5m", "knots": [{"predicted": "0.9", "calibrated": "0.7"}]},
        samples=300,
        markets=80,
        brier_before=Decimal("0.21"),
        brier_after=Decimal("0.19"),
        ece_before=Decimal("0.08"),
        ece_after=Decimal("0.01"),
    )
    await uow.commit()
    assert await uow.calibration.active_fits() == {}


@pytest.mark.parametrize("payout", [Decimal(0), Decimal(1)])
async def test_both_payout_extremes_survive_the_numeric_column(
    uow: SqlUnitOfWork, payout: Decimal
) -> None:
    """``Numeric(10, 8)`` holds 8 decimal places; 1 must not round to 0.99999999."""
    await uow.predictions.record(_prediction("0.5"))
    await uow.resolutions.upsert(
        MarketResolution(
            condition_id="0xaa",  # type: ignore[arg-type]
            payouts=(OutcomePayout(token_id=YES, payout=payout),),  # type: ignore[arg-type]
            resolved_at=NOW,
            status="resolved",
        )
    )
    await uow.commit()
    assert (await uow.predictions.list_samples())[0].realized == payout


async def test_a_batch_writes_every_book_in_one_statement(uow: SqlUnitOfWork) -> None:
    """The batched writer replaced a session round trip per book update (§91).

    Correctness before throughput: a batch must write exactly what the single-row calls
    would have, or the fix quietly changes the history it existed to preserve.
    """
    from deepflow.core.domain import BookLevel, MarketSnapshot, OrderBook

    def _snapshot(condition_id: str, bid: str, ask: str) -> MarketSnapshot:
        return MarketSnapshot(
            condition_id=condition_id,  # type: ignore[arg-type]
            books=(
                OrderBook(
                    token_id=YES,  # type: ignore[arg-type]
                    bids=(BookLevel(price=Decimal(bid), size=Decimal(100)),),
                    asks=(BookLevel(price=Decimal(ask), size=Decimal(100)),),
                    captured_at=NOW,
                ),
                OrderBook(
                    token_id=NO,  # type: ignore[arg-type]
                    bids=(BookLevel(price=Decimal("0.10"), size=Decimal(100)),),
                    asks=(BookLevel(price=Decimal("0.11"), size=Decimal(100)),),
                    captured_at=NOW,
                ),
            ),
            captured_at=NOW,
        )

    written = await uow.snapshots.record_many(
        [_snapshot("0xaa", "0.90", "0.91"), _snapshot("0xbb", "0.80", "0.81")]
    )
    await uow.commit()
    # Two snapshots, two priced books each.
    assert written == 4

    latest = await uow.snapshots.latest(YES)  # type: ignore[arg-type]
    assert latest is not None


async def test_an_unpriced_book_is_still_skipped_in_a_batch(uow: SqlUnitOfWork) -> None:
    """A row saying "observed, no price" reads later exactly like a market that
    genuinely emptied, and the two mean opposite things in a backtest. The batched path
    has to keep the single-row path's judgement, not just its speed."""
    from deepflow.core.domain import MarketSnapshot, OrderBook

    empty = MarketSnapshot(
        condition_id="0xcc",  # type: ignore[arg-type]
        books=(
            OrderBook(token_id=YES, bids=(), asks=(), captured_at=NOW),  # type: ignore[arg-type]
        ),
        captured_at=NOW,
    )
    assert await uow.snapshots.record_many([empty]) == 0
    assert await uow.snapshots.record_many([]) == 0
