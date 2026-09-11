"""Execution engine.

The boundary between a risk-approved signal and the venue. Responsibilities:

* translate a signal plus a sizing result into an :class:`OrderIntent`
* choose the order type
* re-check the breakers immediately before submission
* route to the mode-appropriate executor (live, paper, shadow)

The pre-submission breaker re-check is deliberate. Between risk approval and
submission the book can move, the stream can drop, or a breaker can trip. The
gate's verdict was true when it was computed; this confirms it is still true at
the only moment that matters.
"""

from __future__ import annotations

from decimal import Decimal

from deepflow.config.settings import Settings
from deepflow.core.domain import OrderIntent, OrderRecord, Signal
from deepflow.core.enums import OrderType
from deepflow.core.logging import get_logger
from deepflow.execution.order_manager import OrderManager
from deepflow.risk.circuit_breakers import CircuitBreakerRegistry
from deepflow.risk.sizing import SizingResult

log = get_logger(__name__)


class ExecutionEngine:
    """Builds and dispatches orders."""

    def __init__(
        self,
        *,
        order_manager: OrderManager,
        breakers: CircuitBreakerRegistry,
        settings: Settings,
    ) -> None:
        self._orders = order_manager
        self._breakers = breakers
        self._settings = settings

    def build_intent(self, signal: Signal, sizing: SizingResult) -> OrderIntent:
        """Turn an approved signal into a concrete order.

        TODO(skeleton): shares from stake and price; limit price from the
        signal's target with the slippage bound applied; idempotency key from
        ``execution.idempotency.build_client_key``.
        """
        raise NotImplementedError("ExecutionEngine.build_intent")

    def choose_order_type(self, signal: Signal, *, urgency: Decimal) -> OrderType:
        """Select an order type.

        Default is ``MARKETABLE_LIMIT``: it takes liquidity like a market order
        but carries a worst-price bound, which on a thin outcome book is the
        difference between paying the spread and paying the whole book.

        A true ``MARKET`` order is permitted only when
        ``allow_market_orders`` is set and the situation justifies unbounded
        price risk -- in practice an emergency exit on a confirmed state
        change, where not getting out is the larger risk.
        """
        raise NotImplementedError("ExecutionEngine.choose_order_type")

    async def submit(self, signal: Signal, sizing: SizingResult) -> OrderRecord:
        """Execute an approved signal, re-checking the breakers first."""
        raise NotImplementedError("ExecutionEngine.submit")
