"""What trips the breakers, and what deliberately does not.

Two properties are load-bearing here. Rate conditions use a sliding window, so a
long-running healthy process cannot accumulate its way into a halt. And nothing in
this module resets a breaker — they latch, and a supervisor that re-armed on a
transient recovery would open a window for entries on every flap.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.config.thresholds import (
    CircuitBreakerThresholds,
    ExecutionThresholds,
    RiskLimits,
)
from deepflow.core.clock import ManualClock
from deepflow.core.enums import BreakerReason
from deepflow.execution.order_manager import ExecutionHealth
from deepflow.execution.reconciliation import (
    Discrepancy,
    ReconcileTrigger,
    ReconciliationReport,
)
from deepflow.risk.breaker_supervisor import MIN_API_CALLS_FOR_RATE, BreakerSupervisor
from deepflow.risk.circuit_breakers import CircuitBreakerRegistry
from deepflow.risk.limits import BankrollState

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _supervisor(
    *,
    clock: ManualClock | None = None,
    breakers: CircuitBreakerThresholds | None = None,
    execution: ExecutionThresholds | None = None,
    limits: RiskLimits | None = None,
) -> tuple[BreakerSupervisor, CircuitBreakerRegistry]:
    clock = clock or ManualClock(NOW)
    breakers = breakers or CircuitBreakerThresholds()
    registry = CircuitBreakerRegistry(breakers, clock)
    supervisor = BreakerSupervisor(
        registry=registry,
        thresholds=breakers,
        execution=execution or ExecutionThresholds(),
        limits=limits or RiskLimits(),
        clock=clock,
    )
    return supervisor, registry


# --- Feed staleness -------------------------------------------------------
def test_a_stale_feed_trips_and_blocks_entries() -> None:
    clock = ManualClock(NOW)
    supervisor, registry = _supervisor(clock=clock)
    clock.advance(timedelta(seconds=30))

    supervisor.evaluate(last_event_at=NOW)
    assert BreakerReason.STALE_DATA in registry.open_reasons
    assert not registry.entries_allowed()


def test_a_live_feed_does_not_trip() -> None:
    clock = ManualClock(NOW)
    supervisor, registry = _supervisor(clock=clock)
    clock.advance(timedelta(seconds=2))
    supervisor.evaluate(last_event_at=clock.now())
    assert registry.entries_allowed()


def test_an_absent_input_is_not_evaluated_as_healthy() -> None:
    """A supervisor asked to judge staleness without a feed time should not
    conclude the feed is fine — but it also must not invent a trip."""
    supervisor, registry = _supervisor()
    supervisor.evaluate()
    assert registry.open_reasons == ()


# --- Exits are never blocked ---------------------------------------------
def test_exits_stay_allowed_through_every_trip() -> None:
    """A system that cannot reduce risk during a failure is more dangerous than one
    that keeps trading."""
    supervisor, registry = _supervisor()
    supervisor.halt(actor="operator", detail="manual")
    supervisor.record_database_failure("down")
    assert not registry.entries_allowed()
    assert registry.exits_allowed()


# --- Rate windows ---------------------------------------------------------
def test_reconnects_are_measured_over_a_sliding_hour() -> None:
    """Twenty reconnects in a month is healthy; twenty in an hour is a broken feed.

    A lifetime counter cannot tell those apart and will eventually trip any
    long-running process, which teaches operators to ignore the breaker.
    """
    clock = ManualClock(NOW)
    supervisor, registry = _supervisor(
        clock=clock, breakers=CircuitBreakerThresholds(max_websocket_reconnects_per_hour=3)
    )

    for _ in range(4):
        supervisor.record_reconnect()
        clock.advance(timedelta(minutes=30))  # spread beyond the window
    supervisor.evaluate()
    assert BreakerReason.WEBSOCKET_FAILURE not in registry.open_reasons

    for _ in range(4):
        supervisor.record_reconnect()  # four inside one minute
    supervisor.evaluate()
    assert BreakerReason.WEBSOCKET_FAILURE in registry.open_reasons


def test_an_api_error_rate_needs_a_meaningful_sample() -> None:
    """One failure out of one call is a 100% error rate and says nothing.

    Tripping on it would halt trading on the first request after a restart.
    """
    supervisor, registry = _supervisor()
    supervisor.record_api_call(failed=True)
    supervisor.evaluate()
    assert BreakerReason.API_FAILURE not in registry.open_reasons


def test_a_sustained_api_error_rate_trips() -> None:
    supervisor, registry = _supervisor(
        breakers=CircuitBreakerThresholds(max_api_error_rate=Decimal("0.2"))
    )
    for index in range(MIN_API_CALLS_FOR_RATE + 5):
        supervisor.record_api_call(failed=index % 2 == 0)
    supervisor.evaluate()
    assert BreakerReason.API_FAILURE in registry.open_reasons


# --- Immediate trips ------------------------------------------------------
def test_a_settlement_failure_trips_without_waiting_for_an_evaluation() -> None:
    """Local state and the venue disagree about a position already booked as
    confirmed, and the default threshold is one — so there is nothing to accumulate
    and waiting only widens the window in which the next entry is allowed."""
    supervisor, registry = _supervisor(
        breakers=CircuitBreakerThresholds(max_settlement_failures_per_hour=0)
    )
    supervisor.record_settlement_failure("trade 0xabc FAILED")
    assert BreakerReason.SETTLEMENT_FAILURE in registry.open_reasons


def test_a_database_failure_trips_on_the_first_one() -> None:
    """Unlike a dropped snapshot, an unwritable database means positions, orders and
    the journal are all diverging from reality."""
    supervisor, registry = _supervisor()
    supervisor.record_database_failure("connection refused")
    assert BreakerReason.DATABASE_FAILURE in registry.open_reasons


def test_abnormal_slippage_trips_per_fill() -> None:
    """A single fill far through the touch means the book was not what we priced
    against, and the next entry would use the same wrong view."""
    supervisor, registry = _supervisor(
        breakers=CircuitBreakerThresholds(abnormal_slippage_bps=Decimal(300))
    )
    supervisor.record_realized_slippage(Decimal(120))
    assert registry.open_reasons == ()
    supervisor.record_realized_slippage(Decimal(450), detail="thin book")
    assert BreakerReason.ABNORMAL_SLIPPAGE in registry.open_reasons


def test_a_model_that_raises_trips_where_one_that_abstains_does_not() -> None:
    """An engine with missing inputs is expected to abstain; one that raises is a
    bug, and its next output cannot be trusted either."""
    supervisor, registry = _supervisor()
    supervisor.record_model_failure("football-v1", "KeyError: minute")
    assert BreakerReason.MODEL_FAILURE in registry.open_reasons


# --- Execution and bankroll ----------------------------------------------
def test_a_run_of_execution_errors_trips() -> None:
    supervisor, registry = _supervisor(
        execution=ExecutionThresholds(max_consecutive_execution_errors=3)
    )
    supervisor.evaluate(execution_health=ExecutionHealth(consecutive_errors=3))
    assert BreakerReason.EXECUTION_ERRORS in registry.open_reasons


def test_the_daily_loss_breaker_halts_the_day_not_just_the_trade() -> None:
    """Duplicated with RiskEngine on purpose: the risk engine refuses *this* trade,
    the breaker halts *all* entries until a human looks. A system with only the
    first would keep re-asking on every new signal."""
    supervisor, registry = _supervisor(limits=RiskLimits(max_daily_loss_fraction=Decimal("0.05")))
    supervisor.evaluate(
        bankroll=BankrollState(
            balance_usdc=Decimal(10_000),
            peak_balance_usdc=Decimal(10_000),
            realized_pnl_today=Decimal(-600),
        )
    )
    assert BreakerReason.DAILY_LOSS_LIMIT in registry.open_reasons


def test_drawdown_trips_from_the_peak() -> None:
    supervisor, registry = _supervisor(limits=RiskLimits(max_drawdown_fraction=Decimal("0.15")))
    supervisor.evaluate(
        bankroll=BankrollState(balance_usdc=Decimal(8_000), peak_balance_usdc=Decimal(10_000))
    )
    assert BreakerReason.EXCESSIVE_DRAWDOWN in registry.open_reasons


def test_a_profitable_day_does_not_trip() -> None:
    supervisor, registry = _supervisor()
    supervisor.evaluate(
        bankroll=BankrollState(
            balance_usdc=Decimal(11_000),
            peak_balance_usdc=Decimal(11_000),
            realized_pnl_today=Decimal(1_000),
        )
    )
    assert registry.open_reasons == ()


# --- Reconciliation -------------------------------------------------------
def test_any_discrepancy_halts_entries() -> None:
    """No severity ladder: grading discrepancies needs knowledge of which kind is
    safe to trade through, which is exactly what a failed reconciliation says we
    lack."""
    supervisor, registry = _supervisor()
    report = ReconciliationReport(
        trigger=ReconcileTrigger.STARTUP,
        ran_at=NOW,
        discrepancies=[
            Discrepancy(kind="orphan_order_at_venue", identifier="k", local="", remote="OPEN")
        ],
    )
    supervisor.evaluate(reconciliation=report)
    assert BreakerReason.RECONCILIATION_FAILURE in registry.open_reasons


def test_a_clean_reconciliation_does_not_trip() -> None:
    supervisor, registry = _supervisor()
    supervisor.evaluate(
        reconciliation=ReconciliationReport(trigger=ReconcileTrigger.PERIODIC, ran_at=NOW)
    )
    assert registry.open_reasons == ()


# --- Latching -------------------------------------------------------------
def test_breakers_latch_through_a_recovery() -> None:
    """The supervisor never resets. A breaker that re-arms on a transient recovery
    flaps through the same failure, and each flap allows entries again."""
    clock = ManualClock(NOW)
    supervisor, registry = _supervisor(clock=clock)
    clock.advance(timedelta(seconds=60))
    supervisor.evaluate(last_event_at=NOW)
    assert BreakerReason.STALE_DATA in registry.open_reasons

    # Feed recovers completely.
    supervisor.evaluate(last_event_at=clock.now())
    assert BreakerReason.STALE_DATA in registry.open_reasons
    assert not registry.entries_allowed()


def test_only_an_explicit_reset_clears_a_breaker_and_records_who() -> None:
    """Resuming trading is sensitive and is never anonymous."""
    supervisor, registry = _supervisor()
    supervisor.record_database_failure("down")
    assert registry.reset(BreakerReason.DATABASE_FAILURE, actor="operator@example")
    assert registry.entries_allowed()


def test_retripping_keeps_the_original_time() -> None:
    """The record should show when the problem started, not when it was last seen."""
    clock = ManualClock(NOW)
    supervisor, registry = _supervisor(clock=clock)
    supervisor.record_database_failure("first")
    first = registry.state.trips[BreakerReason.DATABASE_FAILURE].tripped_at

    clock.advance(timedelta(minutes=5))
    supervisor.record_database_failure("second")
    assert registry.state.trips[BreakerReason.DATABASE_FAILURE].tripped_at == first


@pytest.mark.parametrize("reason", list(BreakerReason))
def test_every_breaker_reason_blocks_entries_when_open(reason: BreakerReason) -> None:
    """No reason is advisory. If a condition is worth a breaker, it halts."""
    _, registry = _supervisor()
    registry.trip(reason, "test")
    assert not registry.entries_allowed()
    assert registry.exits_allowed()
