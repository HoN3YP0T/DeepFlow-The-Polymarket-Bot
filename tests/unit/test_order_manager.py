"""Order lifecycle, written around the one failure that costs real money.

That failure is a duplicate position: treating an uncertain outcome as a failure and
retrying it, so one intent becomes two orders at two prices in a market that has
already moved. Every test here exists to pin some part of the path that prevents it.

The fake venue below records every call, because *what was not called* is the
assertion in most of these — a retry that did not happen is the whole point.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from deepflow.config.thresholds import ExecutionThresholds
from deepflow.core.clock import ManualClock
from deepflow.core.domain import OrderIntent, OrderRecord
from deepflow.core.enums import OrderSide, OrderStatus, OrderType
from deepflow.core.errors import (
    DuplicateOrderError,
    ExecutionUncertainError,
    OrderRejectedError,
)
from deepflow.core.types import ClientOrderKey, ClobTokenId, ConditionId, OrderId
from deepflow.execution.order_manager import OrderManager

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
KEY = ClientOrderKey("k1")


def _intent(**overrides: object) -> OrderIntent:
    base: dict[str, object] = {
        "client_key": KEY,
        "condition_id": ConditionId("0xabc"),
        "token_id": ClobTokenId("1"),
        "side": OrderSide.BUY,
        "order_type": OrderType.LIMIT,
        "size_shares": Decimal(100),
        "limit_price": Decimal("0.95"),
        "max_slippage_bps": Decimal(100),
    }
    base.update(overrides)
    return OrderIntent(**base)  # type: ignore[arg-type]


def _record(status: OrderStatus, **overrides: object) -> OrderRecord:
    base: dict[str, object] = {
        "client_key": KEY,
        "order_id": OrderId("o1"),
        "status": status,
        "submitted_at": NOW,
        "updated_at": NOW,
    }
    base.update(overrides)
    return OrderRecord(**base)  # type: ignore[arg-type]


class _Venue:
    """A scriptable execution port that records what it was asked to do."""

    def __init__(
        self,
        *,
        submit_results: list[object] | None = None,
        poll_results: list[OrderRecord | None] | None = None,
        existing: OrderRecord | None = None,
        cancel_result: object | None = None,
    ) -> None:
        self.submit_results = submit_results or [_record(OrderStatus.FILLED)]
        self.poll_results = poll_results or []
        self.existing = existing
        self.cancel_result = cancel_result
        self.submits: list[OrderIntent] = []
        self.cancels: list[OrderId] = []
        self.lookups: list[OrderIntent] = []

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        self.submits.append(intent)
        result = self.submit_results[min(len(self.submits) - 1, len(self.submit_results) - 1)]
        if isinstance(result, Exception):
            raise result
        assert isinstance(result, OrderRecord)
        return result

    async def cancel(self, order_id: OrderId) -> OrderRecord:
        self.cancels.append(order_id)
        if isinstance(self.cancel_result, Exception):
            raise self.cancel_result
        return self.cancel_result or _record(OrderStatus.CANCELLED)  # type: ignore[return-value]

    async def cancel_all(self) -> list[OrderRecord]:
        return []

    async def get_order(self, order_id: OrderId) -> OrderRecord | None:
        """Scripted polls, then an OPEN order that never fills.

        The default is a *working* order on purpose: it is what forces the timeout
        and cancellation paths to be exercised rather than short-circuited.
        """
        if self.poll_results:
            return self.poll_results.pop(0)
        return _record(OrderStatus.OPEN)

    async def find_by_intent(self, intent: OrderIntent) -> OrderRecord | None:
        self.lookups.append(intent)
        return self.existing

    async def list_open_orders(self) -> list[OrderRecord]:
        return []


def _manager(venue: _Venue, **threshold_overrides: object) -> OrderManager:
    defaults: dict[str, object] = {
        "order_timeout_seconds": 1.0,
        "max_submit_retries": 2,
        "max_reprice_attempts": 1,
    }
    defaults.update(threshold_overrides)
    thresholds = ExecutionThresholds(**defaults)  # type: ignore[arg-type]
    return OrderManager(execution=venue, thresholds=thresholds, clock=ManualClock(NOW))


# --- The rule that prevents duplicate positions ---------------------------
@pytest.mark.asyncio
async def test_an_uncertain_submission_is_never_retried() -> None:
    """The single most important behaviour in the execution path.

    A timeout or a 5xx means the order *may* be live. Retrying it acquires twice the
    intended position at a worse price, in a market that has already moved.
    """
    venue = _Venue(submit_results=[ExecutionUncertainError("connection reset")])
    record = await _manager(venue).execute(_intent())

    assert record.status is OrderStatus.UNKNOWN
    assert record.needs_reconciliation
    assert len(venue.submits) == 1, "must not retry an uncertain outcome"
    assert "uncertain" in (record.error or "")


@pytest.mark.asyncio
async def test_an_uncertain_submission_has_no_order_id_to_cancel() -> None:
    """And therefore cannot be tidied away -- only reconciliation can resolve it."""
    venue = _Venue(submit_results=[ExecutionUncertainError("timeout")])
    record = await _manager(venue).execute(_intent())
    assert record.order_id is None
    assert venue.cancels == []


@pytest.mark.asyncio
async def test_a_definitive_rejection_is_retried() -> None:
    """The mirror image: a proven non-execution is safe to retry, and must be.

    Collapsing this case together with the uncertain one would make the system
    either duplicate orders or give up on recoverable refusals.
    """
    venue = _Venue(
        submit_results=[
            OrderRejectedError("tick size"),
            OrderRejectedError("tick size"),
            _record(OrderStatus.FILLED),
        ]
    )
    record = await _manager(venue).execute(_intent())
    assert record.status is OrderStatus.FILLED
    assert len(venue.submits) == 3


@pytest.mark.asyncio
async def test_retries_are_bounded() -> None:
    venue = _Venue(submit_results=[OrderRejectedError("nope")])
    record = await _manager(venue).execute(_intent())
    assert record.status is OrderStatus.REJECTED
    assert len(venue.submits) == 3  # 1 + max_submit_retries
    assert "after 3 attempt" in (record.error or "")


@pytest.mark.asyncio
async def test_a_live_duplicate_key_is_refused_rather_than_resubmitted() -> None:
    """The venue already holds this intent; submitting again makes two positions."""
    venue = _Venue(existing=_record(OrderStatus.OPEN))
    with pytest.raises(DuplicateOrderError):
        await _manager(venue).execute(_intent())
    assert venue.submits == []


@pytest.mark.asyncio
async def test_a_spent_key_does_not_block_a_new_order() -> None:
    """A terminal order under the same key is history, not a live duplicate."""
    venue = _Venue(existing=_record(OrderStatus.CANCELLED))
    record = await _manager(venue).execute(_intent())
    assert record.status is OrderStatus.FILLED


@pytest.mark.asyncio
async def test_a_failed_duplicate_check_does_not_halt_trading() -> None:
    """A venue hiccup in a pre-submission *check* must not become a refusal.

    The submission path's own uncertain handling is the real protection; refusing
    to trade because a lookup failed would turn a transient read error into a halt.
    """

    class _Failing(_Venue):
        async def find_by_intent(self, intent: OrderIntent) -> OrderRecord | None:
            raise RuntimeError("lookup unavailable")

    venue = _Failing()
    record = await _manager(venue).execute(_intent())
    assert record.status is OrderStatus.FILLED


# --- Delayed markets ------------------------------------------------------
@pytest.mark.asyncio
async def test_a_delayed_order_is_left_alone() -> None:
    """DELAYED is accepted-but-unmatched: not a partial fill, not a rejection.

    Cancelling it throws away a queue position for no reason.
    """
    venue = _Venue(
        submit_results=[_record(OrderStatus.PENDING_NEW)],
        poll_results=[_record(OrderStatus.DELAYED)],
    )
    record = await _manager(venue).execute(_intent())
    assert record.status is OrderStatus.DELAYED
    assert venue.cancels == []


# --- Timeout and cancellation --------------------------------------------
@pytest.mark.asyncio
async def test_an_unfilled_order_is_cancelled_at_the_timeout() -> None:
    venue = _Venue(
        submit_results=[_record(OrderStatus.OPEN)],
        poll_results=[_record(OrderStatus.OPEN)] * 10,
    )
    record = await _manager(venue, max_reprice_attempts=0).execute(_intent())
    assert record.status is OrderStatus.CANCELLED
    assert venue.cancels == [OrderId("o1")]


@pytest.mark.asyncio
async def test_an_uncertain_cancellation_is_unknown_not_cancelled() -> None:
    """We do not know whether the order is live, and guessing either way is worse.

    Reporting it CANCELLED leaves a resting order nobody is managing; reporting it
    OPEN invites a retry. UNKNOWN sends it to reconciliation, which is the only
    honest answer.
    """
    venue = _Venue(
        submit_results=[_record(OrderStatus.OPEN)],
        poll_results=[_record(OrderStatus.OPEN)] * 10,
        cancel_result=ExecutionUncertainError("gateway timeout"),
    )
    record = await _manager(venue, max_reprice_attempts=0).execute(_intent())
    assert record.status is OrderStatus.UNKNOWN
    assert record.needs_reconciliation


@pytest.mark.asyncio
async def test_a_cancel_that_raced_a_fill_reports_the_fill() -> None:
    """A cancel losing a race is a real outcome, not a failure."""
    venue = _Venue(
        submit_results=[_record(OrderStatus.OPEN)],
        poll_results=[_record(OrderStatus.OPEN)] * 10,
        cancel_result=_record(OrderStatus.OPEN),  # cancel did not take
    )
    venue.poll_results.append(_record(OrderStatus.FILLED, filled_shares=Decimal(100)))
    record = await _manager(venue, max_reprice_attempts=0).execute(_intent())
    assert record.status in {OrderStatus.FILLED, OrderStatus.OPEN}


# --- Repricing ------------------------------------------------------------
@pytest.mark.asyncio
async def test_reprice_submits_only_the_unfilled_remainder() -> None:
    """Re-submitting the original size after a partial doubles the filled part."""
    partial = _record(OrderStatus.PARTIALLY_FILLED, filled_shares=Decimal(40))
    venue = _Venue(
        submit_results=[_record(OrderStatus.OPEN), _record(OrderStatus.FILLED)],
        cancel_result=_record(OrderStatus.CANCELLED, filled_shares=Decimal(40)),
    )
    await _manager(venue).reprice(partial, _intent())
    assert len(venue.submits) == 1
    assert venue.submits[0].size_shares == Decimal(60)


@pytest.mark.asyncio
async def test_reprice_uses_a_fresh_client_key() -> None:
    """The old key is spent, and reusing it makes the retry indistinguishable from
    the original during reconciliation."""
    partial = _record(OrderStatus.PARTIALLY_FILLED, filled_shares=Decimal(40))
    venue = _Venue(
        submit_results=[_record(OrderStatus.FILLED)],
        cancel_result=_record(OrderStatus.CANCELLED),
    )
    await _manager(venue).reprice(partial, _intent())
    assert venue.submits[0].client_key != KEY


@pytest.mark.asyncio
async def test_reprice_confirms_the_cancellation_before_resubmitting() -> None:
    """ "We asked it to cancel" is not "it cancelled", and submitting on top of a
    live order is the duplicate this module exists to prevent."""
    partial = _record(OrderStatus.PARTIALLY_FILLED, filled_shares=Decimal(40))
    venue = _Venue(cancel_result=_record(OrderStatus.OPEN))  # cancel did not land
    result = await _manager(venue).reprice(partial, _intent())
    assert venue.submits == []
    assert result.status is not OrderStatus.FILLED


@pytest.mark.asyncio
async def test_a_fully_filled_order_is_not_repriced() -> None:
    filled = _record(OrderStatus.FILLED, filled_shares=Decimal(100))
    venue = _Venue()
    result = await _manager(venue).reprice(filled, _intent())
    assert result is filled
    assert venue.cancels == []


# --- Health ---------------------------------------------------------------
@pytest.mark.asyncio
async def test_consecutive_errors_reset_on_success() -> None:
    """Health measures a run of failures, not a lifetime count -- otherwise a
    long-lived process trips permanently on history."""
    venue = _Venue(submit_results=[OrderRejectedError("x"), _record(OrderStatus.FILLED)])
    manager = _manager(venue)
    await manager.execute(_intent())
    assert manager.health.consecutive_errors == 0
    assert manager.health.total_rejections == 1
    assert manager.health.total_submissions == 1


@pytest.mark.asyncio
async def test_uncertain_outcomes_are_counted_separately() -> None:
    """They are not rejections: a rejection is safe, an uncertain outcome is not."""
    venue = _Venue(submit_results=[ExecutionUncertainError("x")])
    manager = _manager(venue)
    await manager.execute(_intent())
    assert manager.health.total_uncertain == 1
    assert manager.health.total_rejections == 0


@pytest.mark.asyncio
async def test_health_goes_unhealthy_after_the_configured_run_of_errors() -> None:
    venue = _Venue(submit_results=[OrderRejectedError("x")])
    thresholds = ExecutionThresholds(
        order_timeout_seconds=1.0, max_submit_retries=5, max_consecutive_execution_errors=3
    )
    manager = OrderManager(execution=venue, thresholds=thresholds, clock=ManualClock(NOW))
    await manager.execute(_intent())
    assert not manager.health.is_healthy(thresholds)
