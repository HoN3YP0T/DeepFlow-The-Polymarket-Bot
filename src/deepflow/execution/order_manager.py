"""Order lifecycle management. Section 19.

Owns a single order from submission to a terminal state: repricing, timeouts,
partial fills, cancellation, and the uncertain-outcome path.

Central rule, enforced here rather than at call sites: an order whose outcome
is unknown is never retried. It transitions to ``UNKNOWN`` and is handed to
reconciliation. Every duplicate-position incident in this class of system comes
from treating a timeout as a failure.
"""

from __future__ import annotations

from dataclasses import dataclass

from deepflow.config.thresholds import ExecutionThresholds
from deepflow.core.clock import Clock
from deepflow.core.domain import OrderIntent, OrderRecord
from deepflow.core.logging import get_logger
from deepflow.ports.execution import ExecutionPort

log = get_logger(__name__)


@dataclass(slots=True)
class ExecutionHealth:
    """Rolling execution quality. Feeds the EXECUTION_HEALTHY safety check."""

    consecutive_errors: int = 0
    total_submissions: int = 0
    total_rejections: int = 0
    total_uncertain: int = 0

    def is_healthy(self, thresholds: ExecutionThresholds) -> bool:
        return self.consecutive_errors < thresholds.max_consecutive_execution_errors


class OrderManager:
    """Drives one order to a terminal state."""

    def __init__(
        self,
        *,
        execution: ExecutionPort,
        thresholds: ExecutionThresholds,
        clock: Clock,
    ) -> None:
        self._execution = execution
        self._thresholds = thresholds
        self._clock = clock
        self._health = ExecutionHealth()

    async def execute(self, intent: OrderIntent) -> OrderRecord:
        """Submit and manage ``intent``.

        TODO(skeleton):
        1. reject a duplicate ``client_key`` outright
        2. submit; on ``ExecutionUncertainError`` mark UNKNOWN and stop -- no
           retry, no exception swallowing
        3. on ``OrderRejectedError`` (definitively not executed) retry up to
           ``max_submit_retries``
        4. await fill up to ``order_timeout_seconds``
        5. on a partial, decide reprice or settle for what filled
        6. reprice up to ``max_reprice_attempts``, re-checking the slippage
           bound each time -- a repriced order that has walked past
           ``max_slippage_bps`` is a different trade and must be abandoned
        7. cancel on timeout, then confirm the cancellation actually landed
        """
        raise NotImplementedError("OrderManager.execute")

    async def reprice(self, record: OrderRecord, intent: OrderIntent) -> OrderRecord:
        """Cancel and re-submit at an improved price, within the slippage bound."""
        raise NotImplementedError("OrderManager.reprice")

    @property
    def health(self) -> ExecutionHealth:
        return self._health
