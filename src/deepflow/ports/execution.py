"""Order execution and account-state ports."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol, runtime_checkable

from deepflow.core.domain import OrderIntent, OrderRecord, Position
from deepflow.core.types import ClientOrderKey, OrderId


@runtime_checkable
class ExecutionPort(Protocol):
    """Places and manages orders.

    Contract note on failure semantics, which the rest of the system depends
    on: implementations must raise ``OrderRejectedError`` only when the venue
    definitively refused, and ``ExecutionUncertainError`` whenever the outcome
    is unknown. Collapsing the two into one generic error is what causes
    duplicate orders.
    """

    async def submit(self, intent: OrderIntent) -> OrderRecord: ...

    async def cancel(self, order_id: OrderId) -> OrderRecord: ...

    async def cancel_all(self) -> Sequence[OrderRecord]: ...

    async def get_order(self, order_id: OrderId) -> OrderRecord | None: ...

    async def find_by_client_key(self, key: ClientOrderKey) -> OrderRecord | None:
        """Resolve an uncertain submission. This is how the system decides
        whether a timed-out order actually landed, before any retry."""
        ...

    async def list_open_orders(self) -> Sequence[OrderRecord]: ...


@runtime_checkable
class AccountPort(Protocol):
    """Authoritative account state, as the venue sees it."""

    async def get_collateral_balance(self) -> Decimal: ...

    async def list_positions(self) -> Sequence[Position]: ...


@runtime_checkable
class RelayerPort(Protocol):
    """Transaction infrastructure for relayer-supported workflows.

    Strictly execution plumbing -- approvals, gasless submission, position
    merges and redemptions. No prediction logic may depend on this port.
    """

    async def ensure_allowances(self) -> bool: ...

    async def redeem_positions(self, condition_ids: Sequence[str]) -> object: ...
