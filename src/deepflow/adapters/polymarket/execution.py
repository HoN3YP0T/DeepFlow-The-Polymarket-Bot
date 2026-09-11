"""Order execution adapter.

Implements :class:`~deepflow.ports.execution.ExecutionPort` and
:class:`~deepflow.ports.execution.AccountPort`.

Confirmed on ``AsyncSecureClient`` (0.10.0): ``create_limit_order``,
``create_market_order``, ``place_limit_order``, ``place_market_order``,
``post_order(s)``, ``cancel_order(s)``, ``cancel_all``,
``cancel_market_orders``, ``list_open_orders``, ``get_order``,
``get_balance_allowance``, ``list_positions``, ``wait_for_order_fill_settlement``.

The mode guard is enforced here, at the last hop before the network. Placing it
at the boundary means no upstream bug -- a mis-set flag, a stale config object,
a test harness -- can produce a live order from a PAPER or SHADOW run.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.domain import OrderIntent, OrderRecord, Position
from deepflow.core.enums import RunMode
from deepflow.core.logging import get_logger
from deepflow.core.types import ClientOrderKey, OrderId

log = get_logger(__name__)


class PolymarketExecution:
    """Live order placement, gated by run mode."""

    def __init__(self, session: PolymarketSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings

    def _assert_live(self, action: str) -> None:
        """Final interlock. Nothing reaches the venue outside LIVE mode."""
        if self._settings.mode is not RunMode.LIVE:
            raise RuntimeError(
                f"refusing to {action}: run mode is {self._settings.mode}, not LIVE"
            )

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        """Sign and post an order.

        TODO(skeleton): build via ``create_limit_order`` / ``create_market_order``
        then ``post_order``.

        Failure semantics that callers depend on:
        * venue refusal              -> ``OrderRejectedError``
        * timeout / dropped response -> ``ExecutionUncertainError``

        The second case must never be retried here. It goes to reconciliation,
        which uses ``find_by_client_key`` to establish whether the order landed.
        """
        self._assert_live("submit order")
        raise NotImplementedError("PolymarketExecution.submit")

    async def cancel(self, order_id: OrderId) -> OrderRecord:
        self._assert_live("cancel order")
        raise NotImplementedError("PolymarketExecution.cancel")

    async def cancel_all(self) -> Sequence[OrderRecord]:
        """Panic button behind the dashboard's Cancel All control."""
        self._assert_live("cancel all orders")
        raise NotImplementedError("PolymarketExecution.cancel_all")

    async def get_order(self, order_id: OrderId) -> OrderRecord | None:
        raise NotImplementedError("PolymarketExecution.get_order")

    async def find_by_client_key(self, key: ClientOrderKey) -> OrderRecord | None:
        """Resolve an uncertain submission.

        TODO(skeleton): scan ``list_open_orders`` and recent ``list_trades`` for
        the intent's key. The SDK does not echo an arbitrary client id, so the
        key is reconstructed from (token id, side, price, size, time window) --
        see ``deepflow.execution.idempotency``.
        """
        raise NotImplementedError("PolymarketExecution.find_by_client_key")

    async def list_open_orders(self) -> Sequence[OrderRecord]:
        raise NotImplementedError("PolymarketExecution.list_open_orders")

    # --- AccountPort ------------------------------------------------------
    async def get_collateral_balance(self) -> Decimal:
        raise NotImplementedError("PolymarketExecution.get_collateral_balance")

    async def list_positions(self) -> Sequence[Position]:
        raise NotImplementedError("PolymarketExecution.list_positions")
