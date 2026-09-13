"""Intent construction, order-type choice, and the paper fill model.

Two themes. First, an intent must describe the *same trade* the gate approved —
which means the limit price, the slippage bound and the idempotency key all derive
from the assessment rather than from separate settings. Second, paper fills must not
flatter live: the simulator is tested for the ways it could be optimistic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from deepflow.config.settings import Settings
from deepflow.config.thresholds import ExecutionThresholds, Thresholds
from deepflow.core.clock import ManualClock
from deepflow.core.domain import (
    BookLevel,
    CostBreakdown,
    EvAssessment,
    FeeSchedule,
    MarketSnapshot,
    OrderBook,
    OrderIntent,
    OrderRecord,
    ProbabilityEstimate,
    Signal,
)
from deepflow.core.enums import (
    BreakerReason,
    MarketCategory,
    OrderSide,
    OrderStatus,
    OrderType,
    SignalAction,
)
from deepflow.core.errors import RiskRejectedError, TradingHaltedError
from deepflow.core.types import ClientOrderKey, ClobTokenId, ConditionId, SignalId
from deepflow.execution.engine import ExecutionEngine
from deepflow.modes.paper import PaperExecutor, ShadowExecutor
from deepflow.risk.circuit_breakers import CircuitBreakerRegistry
from deepflow.risk.sizing import SizingResult

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
YES = ClobTokenId("1")
CID = ConditionId("0xabc")


def _signal(*, slippage_bps: str = "50", target: str = "0.95") -> Signal:
    return Signal(
        signal_id=SignalId("s1"),
        condition_id=CID,
        token_id=YES,
        action=SignalAction.BUY,
        category=MarketCategory.FOOTBALL,
        probability=ProbabilityEstimate(
            token_id=YES,
            model_probability=Decimal("0.98"),
            calibrated_probability=Decimal("0.98"),
            uncertainty=Decimal("0.01"),
            engine="test",
        ),
        ev=EvAssessment(
            token_id=YES,
            market_probability=Decimal(target),
            model_probability=Decimal("0.98"),
            edge=Decimal("0.03"),
            costs=CostBreakdown(fee_bps=Decimal(20), slippage_bps=Decimal(slippage_bps)),
            fill_probability=Decimal(1),
            net_ev=Decimal("0.02"),
            confidence=85,
        ),
        target_price=Decimal(target),
        generated_at=NOW,
        rationale="test",
    )


def _sizing(stake: str = "95") -> SizingResult:
    return SizingResult(
        stake_usdc=Decimal(stake),
        kelly_fraction_raw=Decimal("0.5"),
        kelly_fraction_applied=Decimal("0.02"),
        binding_constraint="max_position_fraction",
    )


class _RecordingManager:
    def __init__(self) -> None:
        self.submitted: list[OrderIntent] = []

    async def execute(self, intent: OrderIntent) -> OrderRecord:
        self.submitted.append(intent)
        return OrderRecord(
            client_key=intent.client_key,
            order_id=None,
            status=OrderStatus.FILLED,
            filled_shares=intent.size_shares,
        )


def _engine(
    *, allow_market: bool = False, breakers: CircuitBreakerRegistry | None = None
) -> tuple[ExecutionEngine, _RecordingManager]:
    settings = Settings(
        thresholds=Thresholds(execution=ExecutionThresholds(allow_market_orders=allow_market))
    )
    manager = _RecordingManager()
    registry = breakers or CircuitBreakerRegistry(settings.thresholds.breakers, ManualClock(NOW))
    engine = ExecutionEngine(
        order_manager=manager,  # type: ignore[arg-type]
        breakers=registry,
        settings=settings,
    )
    return engine, manager


# --- The intent must describe the approved trade --------------------------
def test_the_limit_price_is_the_target_walked_by_the_assessed_slippage() -> None:
    """Not a separate setting: the order and the arithmetic that approved it must
    describe the same trade, or the fill happens at a price EV never saw."""
    engine, _ = _engine()
    intent = engine.build_intent(_signal(slippage_bps="50", target="0.90"), _sizing())
    # 0.90 walked up by 50 bps of 0.90.
    assert intent.limit_price == Decimal("0.90") + Decimal("0.90") * Decimal("0.005")
    assert intent.max_slippage_bps == Decimal(50)


def test_a_buy_limit_never_leaves_the_unit_interval() -> None:
    """A limit at 0 or 1 is rejected outright rather than merely aggressive."""
    engine, _ = _engine()
    intent = engine.build_intent(_signal(slippage_bps="5000", target="0.99"), _sizing())
    assert intent.limit_price < Decimal(1)


def test_shares_come_from_the_approved_stake_at_the_order_price() -> None:
    engine, _ = _engine()
    intent = engine.build_intent(_signal(slippage_bps="0", target="0.95"), _sizing("95"))
    assert intent.size_shares == Decimal(95) / Decimal("0.95")


def test_a_buy_carries_an_all_in_collateral_cap() -> None:
    """The taker fee is charged on top of the notional, so without the cap a
    position sized to the last cent overspends."""
    engine, _ = _engine()
    intent = engine.build_intent(_signal(), _sizing("95"))
    assert intent.max_spend == Decimal(95)


# --- Venue conformance ----------------------------------------------------
def test_prices_and_sizes_are_snapped_to_the_venue_grid_conservatively() -> None:
    """A BUY price rounds down and a size rounds down.

    Both directions make the order cheaper or smaller than intended, never the
    reverse: a sub-tick price is a definitive rejection, and a size rounded up is
    the venue's "not enough balance" error wearing a rounding problem's clothes.
    """
    engine, _ = _engine()
    raw = engine.build_intent(_signal(slippage_bps="37"), _sizing())
    snapped = engine.build_intent(_signal(slippage_bps="37"), _sizing(), tick_size=Decimal("0.01"))
    assert snapped.limit_price <= raw.limit_price
    assert snapped.limit_price == snapped.limit_price.quantize(Decimal("0.01"))
    # Size precision is derived from the price tick, not published separately:
    # venue.PRECISION_BY_TICK maps 0.01 to two decimal places of size.
    assert snapped.size_shares == snapped.size_shares.quantize(Decimal("0.01"))


def test_no_rounding_is_applied_when_the_grid_is_unknown() -> None:
    """Guessing 0.01 for a market quoted in 0.001 rounds a valid price into a worse
    one, so an absent tick size means no rounding rather than a default."""
    engine, _ = _engine()
    intent = engine.build_intent(_signal(slippage_bps="37"), _sizing())
    assert intent.limit_price != intent.limit_price.quantize(Decimal("0.01"))


def test_the_idempotency_key_is_derived_from_the_rounded_intent() -> None:
    """Keying off pre-rounding values lets two intents the venue sees as identical
    produce two keys, and therefore two orders."""
    engine, _ = _engine()
    a = engine.build_intent(_signal(slippage_bps="30"), _sizing(), tick_size=Decimal("0.01"))
    b = engine.build_intent(_signal(slippage_bps="31"), _sizing(), tick_size=Decimal("0.01"))
    assert a.limit_price == b.limit_price
    assert a.client_key == b.client_key


def test_a_working_order_carries_no_gtd_expiry() -> None:
    """The venue's shortest expressible GTD lifetime is ~2 minutes against a
    10-second timeout, so a working order is GTC plus our own cancel."""
    engine, _ = _engine()
    assert engine.build_intent(_signal(), _sizing()).expires_at is None


# --- Order type -----------------------------------------------------------
def test_the_default_order_type_is_a_marketable_limit() -> None:
    """Takes liquidity like a market order, with the worst price bounded — on a thin
    outcome book that is the difference between paying the spread and the book."""
    engine, _ = _engine()
    assert engine.choose_order_type(_signal(), urgency=Decimal(0)) is OrderType.MARKETABLE_LIMIT


def test_urgency_cannot_override_the_market_order_setting() -> None:
    """The situations that feel most urgent are where an unbounded order does the
    most damage, so configuration wins over urgency."""
    engine, _ = _engine(allow_market=False)
    assert engine.choose_order_type(_signal(), urgency=Decimal(1)) is OrderType.MARKETABLE_LIMIT


def test_high_urgency_with_the_setting_on_escalates_to_market() -> None:
    engine, _ = _engine(allow_market=True)
    assert engine.choose_order_type(_signal(), urgency=Decimal(1)) is OrderType.MARKET
    assert engine.choose_order_type(_signal(), urgency=Decimal(0)) is OrderType.MARKETABLE_LIMIT


# --- Submission -----------------------------------------------------------
@pytest.mark.asyncio
async def test_the_breakers_are_rechecked_at_submission() -> None:
    """The gate's verdict was true when computed; this confirms it at the only
    moment that matters."""
    settings = Settings()
    registry = CircuitBreakerRegistry(settings.thresholds.breakers, ManualClock(NOW))
    registry.trip(BreakerReason.STALE_DATA, "feed dropped")
    engine, manager = _engine(breakers=registry)

    with pytest.raises(TradingHaltedError):
        await engine.submit(_signal(), _sizing())
    assert manager.submitted == [], "nothing may be sent once a breaker is open"


@pytest.mark.asyncio
async def test_a_halt_raises_rather_than_returning_a_rejected_record() -> None:
    """Nothing was submitted, so a record would put a fictional order into the
    journal and the reconciler's view."""
    settings = Settings()
    registry = CircuitBreakerRegistry(settings.thresholds.breakers, ManualClock(NOW))
    registry.trip(BreakerReason.STALE_DATA)
    engine, _ = _engine(breakers=registry)
    with pytest.raises(TradingHaltedError):
        await engine.submit(_signal(), _sizing())


@pytest.mark.asyncio
async def test_a_zero_share_intent_is_refused_before_submission() -> None:
    engine, manager = _engine()
    with pytest.raises(RiskRejectedError):
        await engine.submit(_signal(), _sizing("0"))
    assert manager.submitted == []


@pytest.mark.asyncio
async def test_a_clean_submission_reaches_the_order_manager() -> None:
    engine, manager = _engine()
    record = await engine.submit(_signal(), _sizing())
    assert record.status is OrderStatus.FILLED
    assert len(manager.submitted) == 1


# --- Paper fills ----------------------------------------------------------
def _book(*asks: tuple[str, str], bid: str = "0.94") -> OrderBook:
    return OrderBook(
        token_id=YES,
        bids=(BookLevel(price=Decimal(bid), size=Decimal(1000)),),
        asks=tuple(BookLevel(price=Decimal(p), size=Decimal(s)) for p, s in asks),
        captured_at=NOW,
    )


def _snapshot(book: OrderBook) -> MarketSnapshot:
    return MarketSnapshot(condition_id=CID, books=(book,), captured_at=NOW)


def _paper_intent(*, size: str = "100", limit: str = "0.96") -> OrderIntent:
    return OrderIntent(
        client_key=ClientOrderKey("k1"),
        condition_id=CID,
        token_id=YES,
        side=OrderSide.BUY,
        order_type=OrderType.MARKETABLE_LIMIT,
        size_shares=Decimal(size),
        limit_price=Decimal(limit),
        max_slippage_bps=Decimal(100),
        max_spend=Decimal(100),
    )


@pytest.mark.asyncio
async def test_paper_fills_at_the_volume_weighted_price_not_the_touch() -> None:
    """The same walk the EV engine priced, so simulated and assessed cost agree."""
    executor = PaperExecutor(ManualClock(NOW))
    executor.observe(_snapshot(_book(("0.95", "50"), ("0.96", "50"))))
    record = await executor.submit(_paper_intent(size="100"))

    assert record.status is OrderStatus.FILLED
    assert record.average_fill_price == Decimal("0.955")
    assert record.average_fill_price > Decimal("0.95"), "never better than the touch"


@pytest.mark.asyncio
async def test_paper_partial_fills_when_depth_runs_out() -> None:
    """Assuming the remainder is how paper flatters live — and the partial path is
    the one reconciliation and position accounting both have to handle."""
    executor = PaperExecutor(ManualClock(NOW))
    executor.observe(_snapshot(_book(("0.95", "30"))))
    record = await executor.submit(_paper_intent(size="100"))

    assert record.status is OrderStatus.PARTIALLY_FILLED
    assert record.filled_shares == Decimal(30)


@pytest.mark.asyncio
async def test_paper_does_not_fill_above_the_limit_price() -> None:
    executor = PaperExecutor(ManualClock(NOW))
    executor.observe(_snapshot(_book(("0.99", "1000"))))
    record = await executor.submit(_paper_intent(limit="0.96"))
    assert record.status is OrderStatus.OPEN
    assert record.filled_shares == 0


@pytest.mark.asyncio
async def test_a_resting_order_is_not_simulated_as_filling() -> None:
    """A non-crossing order waits for a counterparty to come to us, and a snapshot
    cannot say whether that happens.

    The distinction this rests on: queue position applies to our own side of the
    book. Depth on the opposite side is ours to take, all of it — there is no queue
    in front of a taker, which is why the crossing test above fills at 0.96.
    """
    executor = PaperExecutor(ManualClock(NOW))
    executor.observe(_snapshot(_book(("0.97", "1000"))))
    record = await executor.submit(_paper_intent(limit="0.96"))
    assert record.status is OrderStatus.OPEN
    assert record.filled_shares == 0


@pytest.mark.asyncio
async def test_paper_refuses_to_invent_a_fill_without_a_book() -> None:
    """The one output that would make a paper run actively misleading."""
    executor = PaperExecutor(ManualClock(NOW))
    record = await executor.submit(_paper_intent())
    assert record.status is OrderStatus.REJECTED
    assert "invent" in (record.error or "")


@pytest.mark.asyncio
async def test_paper_order_ids_cannot_be_mistaken_for_venue_ids() -> None:
    executor = PaperExecutor(ManualClock(NOW))
    executor.observe(_snapshot(_book(("0.95", "1000"))))
    record = await executor.submit(_paper_intent())
    assert str(record.order_id).startswith("paper-")


@pytest.mark.asyncio
async def test_paper_charges_the_markets_own_fee_schedule() -> None:
    """A paper run with no fee is optimistic by the whole fee, which in the
    0.85-0.98 band is a large fraction of the edge."""
    executor = PaperExecutor(ManualClock(NOW))
    executor.observe(
        _snapshot(_book(("0.95", "1000"))), fee_schedule=FeeSchedule(rate=Decimal("0.04"))
    )
    fee = executor._fee(YES, shares=Decimal(100), price=Decimal("0.95"))
    assert fee > 0


# --- Shadow ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_shadow_records_what_would_have_been_sent_and_sends_nothing() -> None:
    executor = ShadowExecutor(ManualClock(NOW))
    record = await executor.submit(_paper_intent(limit="0.96"))
    assert len(executor.suppressed) == 1
    assert record.status is OrderStatus.OPEN, "not FILLED: nothing was sent"


@pytest.mark.asyncio
async def test_shadow_rejects_an_order_finer_than_the_venue_grid() -> None:
    """The class of failure paper trading structurally cannot catch."""
    executor = ShadowExecutor(ManualClock(NOW))
    record = await executor.submit(_paper_intent(limit="0.96125"))
    assert record.status is OrderStatus.REJECTED
    assert "finer than" in (record.error or "")


@pytest.mark.asyncio
async def test_shadow_rejects_a_buy_without_a_collateral_cap() -> None:
    intent = _paper_intent().model_copy(update={"max_spend": None})
    executor = ShadowExecutor(ManualClock(NOW))
    record = await executor.submit(intent)
    assert record.status is OrderStatus.REJECTED
    assert "max_spend" in (record.error or "")
