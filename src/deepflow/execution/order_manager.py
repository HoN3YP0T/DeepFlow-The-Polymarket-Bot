"""Order lifecycle management. Section 19.

Owns a single order from submission to a terminal state: repricing, timeouts,
partial fills, cancellation, and the uncertain-outcome path.

Central rule, enforced here rather than at call sites: an order whose outcome
is unknown is never retried. It transitions to ``UNKNOWN`` and is handed to
reconciliation. Every duplicate-position incident in this class of system comes
from treating a timeout as a failure.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from decimal import Decimal
from math import ceil
from typing import Final

from deepflow.config.thresholds import ExecutionThresholds
from deepflow.core.clock import Clock
from deepflow.core.domain import OrderIntent, OrderRecord
from deepflow.core.enums import OrderStatus
from deepflow.core.errors import (
    DuplicateOrderError,
    ExecutionUncertainError,
    OrderRejectedError,
)
from deepflow.core.logging import get_logger
from deepflow.core.types import ClientOrderKey
from deepflow.ports.execution import ExecutionPort

log = get_logger(__name__)

BPS: Final = Decimal(10_000)

#: How often an in-flight order is re-read. Fast enough that a ten-second timeout
#: has room to observe a fill, slow enough not to spend the rate limit on polling.
POLL_INTERVAL_SECONDS: Final = 0.5

#: Statuses from which an order will not change again on its own.
#:
#: ``MATCHED_UNSETTLED`` is deliberately **not** here: a matched trade can still
#: end FAILED, so settlement is followed separately and booking a position at match
#: time is how a phantom position enters the ledger.
_TERMINAL_STATES: Final = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
    }
)

_CANCELLED_STATES: Final = frozenset({OrderStatus.CANCELLED})


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
        """Submit and manage ``intent`` to a terminal state.

        The uncertain path is written first and read first, because it is the one
        that costs money when wrong. Everything else here is bookkeeping.

        Order of operations:

        1. refuse a ``client_key`` already live at the venue
        2. submit -- and on an uncertain outcome stop immediately, marked
           ``UNKNOWN``, with no retry and no swallowed exception
        3. retry only a *definitive* rejection, and only while the intent is
           unchanged
        4. wait for a fill up to ``order_timeout_seconds``
        5. reprice a partial within the slippage bound, or settle for what filled
        6. cancel on timeout, then confirm the cancellation actually landed
        """
        existing = await self._find_existing(intent)
        if existing is not None:
            # Not an error to be retried at a different price: the venue already
            # holds this intent, and submitting again is how one intent becomes two
            # positions. The caller gets the live record and decides.
            log.warning(
                "order.duplicate_key",
                client_key=str(intent.client_key),
                status=str(existing.status),
            )
            raise DuplicateOrderError(
                f"client_key {intent.client_key} already exists at the venue "
                f"with status {existing.status}"
            )

        record = await self._submit_with_retries(intent)
        if record.status is OrderStatus.UNKNOWN:
            return record

        return await self._work_order(record, intent)

    async def reprice(self, record: OrderRecord, intent: OrderIntent) -> OrderRecord:
        """Cancel and re-submit the unfilled remainder at an improved price.

        Two rules make this safe rather than a way to chase a market:

        * **The remainder only.** Re-submitting the original size after a partial
          fill doubles the filled portion. The new intent carries
          ``size_shares - filled_shares``.
        * **A new client key.** The old key is spent -- the venue holds a
          cancelled order under it -- and reusing it would make the retry
          indistinguishable from the original in reconciliation.

        Cancellation is confirmed before re-submitting. A reprice that submits on
        top of an order still live at the venue is the duplicate this whole module
        exists to prevent, and "we asked it to cancel" is not the same as
        "it cancelled".

        Returns the **freshly submitted** record without working it to a terminal
        state. That is deliberate: an earlier version called back into
        :meth:`_work_order`, which could reprice again with the attempt counter
        reset at each level, so the reprice budget bounded nothing and the recursion
        did not terminate. The working loop belongs in one place, and this method is
        one step inside it.
        """
        remaining = intent.size_shares - record.filled_shares
        if remaining <= 0:
            return record

        cancelled = await self._cancel_and_confirm(record)
        if cancelled.status not in _CANCELLED_STATES:
            log.warning(
                "order.reprice_abandoned",
                reason="cancellation unconfirmed",
                client_key=str(intent.client_key),
                status=str(cancelled.status),
            )
            return cancelled

        replacement = intent.model_copy(
            update={
                "client_key": ClientOrderKey(f"{intent.client_key}-r{record.filled_shares}"),
                "size_shares": remaining,
            }
        )
        return await self._submit_with_retries(replacement)

    # --- Submission -------------------------------------------------------
    async def _submit_with_retries(self, intent: OrderIntent) -> OrderRecord:
        """Submit, retrying only what is proven not to have executed.

        ``ExecutionUncertainError`` ends this immediately. That is the single most
        important line in the execution path: a timeout, a 5xx or a dropped
        connection means the order *may* be live, and retrying it is how a system
        acquires twice the intended position at a worse price in a market that has
        already moved. The order becomes ``UNKNOWN`` and reconciliation owns it.
        """
        attempts = self._thresholds.max_submit_retries + 1
        last_error = ""
        for attempt in range(attempts):
            try:
                record = await self._execution.submit(intent)
            except ExecutionUncertainError as exc:
                self._health.total_uncertain += 1
                self._health.consecutive_errors += 1
                log.error(
                    "order.uncertain",
                    client_key=str(intent.client_key),
                    attempt=attempt,
                    detail=str(exc),
                )
                return OrderRecord(
                    client_key=intent.client_key,
                    order_id=None,
                    status=OrderStatus.UNKNOWN,
                    submitted_at=self._clock.now(),
                    updated_at=self._clock.now(),
                    error=f"uncertain submission: {exc}",
                )
            except OrderRejectedError as exc:
                # Definitively not executed, so retrying cannot duplicate.
                self._health.total_rejections += 1
                self._health.consecutive_errors += 1
                last_error = str(exc)
                log.warning(
                    "order.rejected",
                    client_key=str(intent.client_key),
                    attempt=attempt,
                    detail=last_error,
                )
                continue

            self._health.total_submissions += 1
            self._health.consecutive_errors = 0
            return record

        return OrderRecord(
            client_key=intent.client_key,
            order_id=None,
            status=OrderStatus.REJECTED,
            submitted_at=self._clock.now(),
            updated_at=self._clock.now(),
            error=f"rejected after {attempts} attempt(s): {last_error}",
        )

    async def _find_existing(self, intent: OrderIntent) -> OrderRecord | None:
        """Whether the venue already holds this intent.

        A lookup failure returns ``None`` rather than raising: this is a
        pre-submission guard, and the submission path's own uncertain handling is
        the real protection. Refusing to trade because a *check* failed would turn
        a venue hiccup into a halt.
        """
        try:
            found = await self._execution.find_by_client_key(intent.client_key)
        except Exception:
            log.warning("order.duplicate_check_failed", client_key=str(intent.client_key))
            return None
        if found is None or found.status in _TERMINAL_STATES:
            return None
        return found

    # --- Working the order ------------------------------------------------
    async def _work_order(self, record: OrderRecord, intent: OrderIntent) -> OrderRecord:
        """Wait, reprice, or cancel -- until the order is terminal.

        The single working loop. Each pass waits out the client-side timeout and
        then decides: terminal, leave alone, reprice the remainder, or cancel. The
        reprice budget is consumed here and nowhere else, so it actually bounds the
        number of attempts.
        """
        working = intent
        for attempt in range(self._thresholds.max_reprice_attempts + 1):
            # Checked before polling, not after. A submit that already came back
            # terminal needs no round trip, and re-reading it risks overwriting a
            # known outcome with a staler view of the same order.
            if record.status not in _TERMINAL_STATES:
                record = await self._await_terminal_or_timeout(record)

            if record.status in _TERMINAL_STATES:
                return record
            if record.status is OrderStatus.DELAYED:
                # The venue accepted it and will not match it yet. Not a partial
                # fill and not a rejection; cancelling would throw away a queue
                # position for no reason.
                log.info("order.delayed", client_key=str(intent.client_key))
                return record

            if attempt >= self._thresholds.max_reprice_attempts:
                break
            if not self._worth_repricing(record, working):
                break
            log.info(
                "order.repricing",
                client_key=str(working.client_key),
                attempt=attempt,
                filled=str(record.filled_shares),
            )
            remaining = working.size_shares - record.filled_shares
            record = await self.reprice(record, working)
            if record.status in _TERMINAL_STATES or record.status is OrderStatus.UNKNOWN:
                return record
            # Subsequent passes work the replacement, not the original: its key and
            # its size both changed, and polling the old one would read a cancelled
            # order forever.
            working = working.model_copy(
                update={"client_key": record.client_key, "size_shares": remaining}
            )

        return await self._cancel_and_confirm(record)

    async def _await_terminal_or_timeout(self, record: OrderRecord) -> OrderRecord:
        """Poll until the order is terminal or the client-side timeout expires.

        Client-side because it has to be: the venue's shortest expressible GTD
        lifetime is about two minutes, and the configured timeout is ten seconds,
        so a working order is GTC plus our own cancel. There is no way to delegate
        this.

        Bounded by a **poll budget as well as the clock**, and the redundancy is
        deliberate. A clock that stops advancing -- a suspended container, a step
        backwards from NTP -- would otherwise leave this loop polling an order
        forever while the timeout never expires. The budget makes the loop terminate
        on its own terms, which also means it is testable against a frozen clock
        rather than only against a real one.
        """
        if record.order_id is None:
            return record

        interval = min(POLL_INTERVAL_SECONDS, self._thresholds.order_timeout_seconds)
        budget = max(1, ceil(self._thresholds.order_timeout_seconds / interval))
        deadline = self._clock.now().timestamp() + self._thresholds.order_timeout_seconds

        for _ in range(budget):
            if self._clock.now().timestamp() >= deadline:
                break
            await asyncio.sleep(interval)
            try:
                latest = await self._execution.get_order(record.order_id)
            except Exception:
                # A failed *read* says nothing about the order. Keep polling; the
                # timeout below is the backstop.
                log.warning("order.poll_failed", order_id=str(record.order_id))
                continue
            if latest is None:
                continue
            record = latest
            if record.status in _TERMINAL_STATES or record.status is OrderStatus.DELAYED:
                return record
        return record

    def _worth_repricing(self, record: OrderRecord, intent: OrderIntent) -> bool:
        """Whether a repriced order would still be the trade that was approved.

        A reprice walks the price against us. Once the walk exceeds
        ``max_slippage_bps`` the order is a different trade from the one the gate
        approved and the EV engine priced, and it must be abandoned rather than
        improved -- chasing a market is how an approved edge becomes a loss with a
        full audit trail behind it.
        """
        if intent.limit_price <= 0:
            return False
        drift_bps = (
            abs(record.average_fill_price - intent.limit_price) / intent.limit_price * BPS
            if record.average_fill_price is not None
            else Decimal(0)
        )
        return drift_bps <= intent.max_slippage_bps

    async def _cancel_and_confirm(self, record: OrderRecord) -> OrderRecord:
        """Cancel, then verify the cancellation landed.

        "We sent a cancel" is not "the order is gone". An unconfirmed cancel leaves
        a resting order nobody is managing, which is the state a killed process
        leaves behind and the reason the venue-side heartbeat exists.

        An uncertain cancel is ``UNKNOWN``, exactly like an uncertain submit: we do
        not know whether the order is live, and guessing in either direction is
        worse than saying so.
        """
        if record.order_id is None:
            return record
        try:
            cancelled = await self._execution.cancel(record.order_id)
        except ExecutionUncertainError as exc:
            self._health.total_uncertain += 1
            log.error("order.cancel_uncertain", order_id=str(record.order_id), detail=str(exc))
            return record.model_copy(
                update={
                    "status": OrderStatus.UNKNOWN,
                    "error": f"uncertain cancellation: {exc}",
                    "updated_at": self._clock.now(),
                }
            )

        if cancelled.status in _CANCELLED_STATES or cancelled.status in _TERMINAL_STATES:
            return cancelled

        # The venue answered and the order is still not cancelled. Re-read once;
        # a cancel that raced a fill shows up here as FILLED, which is a real
        # outcome rather than a failure.
        latest = await self._execution.get_order(record.order_id)
        return latest or cancelled

    @property
    def health(self) -> ExecutionHealth:
        return self._health
