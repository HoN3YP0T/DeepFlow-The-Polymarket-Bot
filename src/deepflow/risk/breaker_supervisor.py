"""What actually trips the circuit breakers. Section 21.

:class:`~deepflow.risk.circuit_breakers.CircuitBreakerRegistry` is the halt
*authority* -- it holds the open/closed state and answers "may we enter". It has no
opinion about when to open, on purpose: a registry that also decided would have to
know about streams, orders, bankrolls and the database, and every caller consulting
it would be importing that graph.

This module is the other half: it observes, counts, and decides. The split means the
conditions can be tested without a venue and the authority can be consulted without
one.

Two design rules run through it.

**Rate-limited conditions use a sliding window, not a counter.** Twenty reconnects
over a month is a healthy process; twenty in an hour is a broken feed. A lifetime
counter cannot tell those apart and will eventually trip on any long-running system,
which teaches operators to ignore the breaker.

**Nothing here resets a breaker.** Breakers latch (``auto_resume`` off) and a human
decides the cause is fixed. A supervisor that re-armed on a transient recovery would
flap through the same failure repeatedly -- and each flap is a window in which
entries are allowed again.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

from deepflow.config.thresholds import CircuitBreakerThresholds, ExecutionThresholds, RiskLimits
from deepflow.core.clock import Clock
from deepflow.core.enums import BreakerReason
from deepflow.core.logging import get_logger
from deepflow.execution.order_manager import ExecutionHealth
from deepflow.execution.reconciliation import ReconciliationReport
from deepflow.risk.circuit_breakers import CircuitBreakerRegistry
from deepflow.risk.limits import BankrollState

log = get_logger(__name__)

#: Window over which the "per hour" thresholds are measured.
RATE_WINDOW: Final = timedelta(hours=1)

#: Minimum API calls before an error *rate* means anything. One failure out of one
#: call is a 100% error rate and says nothing; tripping on it would halt trading on
#: the first request after a restart.
MIN_API_CALLS_FOR_RATE: Final = 20


@dataclass(slots=True)
class _Window:
    """Timestamps inside the rate window, oldest first."""

    events: deque[datetime] = field(default_factory=deque)

    def record(self, at: datetime) -> None:
        self.events.append(at)

    def count(self, *, now: datetime) -> int:
        cutoff = now - RATE_WINDOW
        while self.events and self.events[0] < cutoff:
            self.events.popleft()
        return len(self.events)


class BreakerSupervisor:
    """Turns observations into breaker trips."""

    def __init__(
        self,
        *,
        registry: CircuitBreakerRegistry,
        thresholds: CircuitBreakerThresholds,
        execution: ExecutionThresholds,
        limits: RiskLimits,
        clock: Clock,
    ) -> None:
        self._registry = registry
        self._thresholds = thresholds
        self._execution = execution
        self._limits = limits
        self._clock = clock

        self._reconnects = _Window()
        self._settlement_failures = _Window()
        self._api_errors = _Window()
        self._api_calls = _Window()
        self._database_failures = _Window()

    # --- Observations -----------------------------------------------------
    def record_reconnect(self) -> None:
        """One stream reconnect. Evaluated against the per-hour limit."""
        self._reconnects.record(self._clock.now())

    def record_settlement_failure(self, detail: str = "") -> None:
        """A matched trade that failed to settle on chain.

        Trips immediately rather than at the next evaluation. A failed settlement
        means local state and the venue disagree about a position we had already
        booked as confirmed, and the default threshold is one -- so there is nothing
        to accumulate and waiting for a scheduled check only widens the window in
        which the next entry is allowed.
        """
        self._settlement_failures.record(self._clock.now())
        count = self._settlement_failures.count(now=self._clock.now())
        if count > self._thresholds.max_settlement_failures_per_hour:
            self._registry.trip(
                BreakerReason.SETTLEMENT_FAILURE,
                f"{count} settlement failure(s) in the last hour: {detail}".strip(),
            )

    def record_api_call(self, *, failed: bool) -> None:
        """One venue API call and whether it failed. Drives the error *rate*."""
        now = self._clock.now()
        self._api_calls.record(now)
        if failed:
            self._api_errors.record(now)

    def record_database_failure(self, detail: str = "") -> None:
        """A persistence failure.

        Trips on the first one. Unlike a dropped snapshot -- which costs history --
        a database that cannot be written to means positions, orders and the journal
        are all diverging from reality, and the system can no longer establish what
        it owns.
        """
        self._database_failures.record(self._clock.now())
        self._registry.trip(BreakerReason.DATABASE_FAILURE, detail)

    def record_realized_slippage(self, bps: Decimal, *, detail: str = "") -> None:
        """Slippage actually paid on a fill.

        Compared against the threshold immediately, because the signal is per-fill
        rather than a rate: a single fill 300 bps through the touch means the book
        was not what we priced against, and the next entry would be priced against
        the same wrong view.
        """
        if bps > self._thresholds.abnormal_slippage_bps:
            self._registry.trip(
                BreakerReason.ABNORMAL_SLIPPAGE,
                f"{bps:.0f}bps realized against a {self._thresholds.abnormal_slippage_bps}bps "
                f"limit: {detail}".strip(),
            )

    def record_model_failure(self, engine: str, detail: str = "") -> None:
        """A probability engine that raised rather than abstaining.

        An engine with missing inputs is expected to abstain; one that raises is a
        bug, and its next output cannot be trusted either.
        """
        self._registry.trip(BreakerReason.MODEL_FAILURE, f"{engine}: {detail}".strip())

    def record_invalid_resolution_state(self, condition_id: str, detail: str = "") -> None:
        """A market whose resolution state contradicts what we acted on."""
        self._registry.trip(
            BreakerReason.INVALID_RESOLUTION_STATE, f"{condition_id}: {detail}".strip()
        )

    def halt(self, *, actor: str, detail: str = "") -> None:
        """Manual halt. ``actor`` is recorded, because halting is never anonymous."""
        self._registry.trip(BreakerReason.MANUAL_HALT, f"by {actor}: {detail}".strip())

    # --- Periodic evaluation ---------------------------------------------
    def evaluate(
        self,
        *,
        last_event_at: datetime | None = None,
        execution_health: ExecutionHealth | None = None,
        bankroll: BankrollState | None = None,
        reconciliation: ReconciliationReport | None = None,
    ) -> tuple[BreakerReason, ...]:
        """Check every rate- and state-based condition. Returns what is now open.

        Every argument is optional and an absent one is **not evaluated** rather
        than treated as healthy. A supervisor asked to judge feed staleness without
        being given a feed time should not conclude the feed is fine.

        That does leave one gap worth naming rather than hiding: a caller that
        forgets to pass ``last_event_at`` gets no staleness check at all, and the
        absence looks identical to a healthy feed from here. The orchestrator's
        health loop always passes it, and a stream that has never produced an event
        reports ``None`` -- which is why the caller, not this method, decides whether
        "no events yet" means starting up or broken.
        """
        now = self._clock.now()

        if last_event_at is not None:
            self._evaluate_feed_age(last_event_at, now=now)
        self._evaluate_reconnect_rate(now=now)
        self._evaluate_api_error_rate(now=now)
        if execution_health is not None:
            self._evaluate_execution(execution_health)
        if bankroll is not None:
            self._evaluate_bankroll(bankroll)
        if reconciliation is not None:
            self._evaluate_reconciliation(reconciliation)

        return self._registry.open_reasons

    def _evaluate_feed_age(self, last_event_at: datetime, *, now: datetime) -> None:
        age = (now - last_event_at).total_seconds()
        if age > self._thresholds.max_data_age_seconds:
            self._registry.trip(
                BreakerReason.STALE_DATA,
                f"no stream event for {age:.1f}s (budget {self._thresholds.max_data_age_seconds}s)",
            )

    def _evaluate_reconnect_rate(self, *, now: datetime) -> None:
        count = self._reconnects.count(now=now)
        if count > self._thresholds.max_websocket_reconnects_per_hour:
            self._registry.trip(
                BreakerReason.WEBSOCKET_FAILURE,
                f"{count} reconnects in the last hour "
                f"(limit {self._thresholds.max_websocket_reconnects_per_hour})",
            )

    def _evaluate_api_error_rate(self, *, now: datetime) -> None:
        calls = self._api_calls.count(now=now)
        if calls < MIN_API_CALLS_FOR_RATE:
            # One failure out of one call is a 100% error rate and means nothing.
            return
        errors = self._api_errors.count(now=now)
        rate = Decimal(errors) / Decimal(calls)
        if rate > self._thresholds.max_api_error_rate:
            self._registry.trip(
                BreakerReason.API_FAILURE,
                f"{errors}/{calls} calls failed ({rate:.0%}) against a "
                f"{self._thresholds.max_api_error_rate:.0%} limit",
            )

    def _evaluate_execution(self, health: ExecutionHealth) -> None:
        if not health.is_healthy(self._execution):
            self._registry.trip(
                BreakerReason.EXECUTION_ERRORS,
                f"{health.consecutive_errors} consecutive errors "
                f"(limit {self._execution.max_consecutive_execution_errors})",
            )

    def _evaluate_bankroll(self, bankroll: BankrollState) -> None:
        """Daily loss and drawdown.

        Duplicated with :class:`~deepflow.risk.limits.RiskEngine`, which also refuses
        a trade on both, and the duplication is intentional. The risk engine refuses
        *this* trade; the breaker halts *all* entries until a human looks. One is a
        decision about a position and the other is a decision about the day, and a
        system that only had the first would keep re-asking on every new signal.
        """
        if bankroll.balance_usdc > 0:
            loss_fraction = max(Decimal(0), -bankroll.realized_pnl_today / bankroll.balance_usdc)
            if loss_fraction >= self._limits.max_daily_loss_fraction:
                self._registry.trip(
                    BreakerReason.DAILY_LOSS_LIMIT,
                    f"realized loss {loss_fraction:.1%} of bankroll "
                    f"(limit {self._limits.max_daily_loss_fraction:.1%})",
                )

        if bankroll.drawdown_fraction >= self._limits.max_drawdown_fraction:
            self._registry.trip(
                BreakerReason.EXCESSIVE_DRAWDOWN,
                f"drawdown {bankroll.drawdown_fraction:.1%} from peak "
                f"(limit {self._limits.max_drawdown_fraction:.1%})",
            )

    def _evaluate_reconciliation(self, report: ReconciliationReport) -> None:
        """Any discrepancy halts entries.

        No severity ladder, deliberately. A system that does not know its own
        position size cannot size the next trade or compute exposure, and grading
        discrepancies would require knowing which kind is safe to trade through --
        which is exactly the knowledge a failed reconciliation says we lack.
        """
        if not report.succeeded:
            kinds = ", ".join(sorted({d.kind for d in report.discrepancies}))
            self._registry.trip(
                BreakerReason.RECONCILIATION_FAILURE,
                f"{len(report.discrepancies)} discrepancy(ies) on {report.trigger}: {kinds}",
            )
