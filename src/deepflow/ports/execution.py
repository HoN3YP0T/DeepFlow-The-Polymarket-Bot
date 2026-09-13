"""Order execution and account-state ports."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Protocol, runtime_checkable

from deepflow.core.domain import ClosedFill, OrderIntent, OrderRecord, Position
from deepflow.core.types import OrderId


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

    async def find_by_intent(self, intent: OrderIntent) -> OrderRecord | None:
        """Resolve an uncertain submission. This is how the system decides
        whether a timed-out order actually landed, before any retry.

        Takes the **intent**, not the client key, and the reason is a venue fact
        rather than a preference: Polymarket's CLOB accepts no client-supplied order
        id. ``client_order_id`` exists on the perps API and nowhere else, the order
        creation call has no such parameter, and an open order comes back carrying
        only the venue's own id. So the client key is a purely local fingerprint --
        the venue has never seen it and cannot be asked about it.

        What an implementation can do is recompute the fingerprint's *material* --
        token, side, price, size, and the time bucket -- and match that against the
        orders the venue is working. The intent carries all of it; the key, being a
        hash, carries none of it recoverably.
        """
        ...

    async def list_open_orders(self) -> Sequence[OrderRecord]: ...


@runtime_checkable
class AccountPort(Protocol):
    """Authoritative account state, as the venue sees it."""

    async def get_collateral_balance(self) -> Decimal: ...

    async def list_positions(self) -> Sequence[Position]: ...


@runtime_checkable
class PositionCloserPort(Protocol):
    """Reduces or closes an existing position.

    Narrow on purpose. The position manager's job is position *state* -- what is held,
    what it is worth, what should happen to it -- and giving it the whole execution
    engine would drag tick grids, fee schedules and order typing into a class that
    should not know about any of them. This is the one verb it needs.

    ``urgent`` is the emergency path: the caller has established that the thesis is void,
    so crossing the spread costs less than staying in. Everywhere else in this system
    the trade-off runs the other way, which is why it is an explicit argument rather
    than a judgement made inside the implementation.

    Returns the shares actually closed and the price they closed at, or ``None`` when
    nothing closed. ``None`` is not a failure to report upward -- a partial or absent
    fill is normal -- but it must never be reported as a full close, because the
    position is still open and still needs watching.
    """

    async def close(
        self, position: Position, *, fraction: Decimal, urgent: bool = False
    ) -> ClosedFill | None: ...


@runtime_checkable
class RelayerPort(Protocol):
    """Transaction infrastructure for relayer-supported workflows.

    Strictly execution plumbing -- approvals, gasless submission, position
    merges and redemptions. No prediction logic may depend on this port.
    """

    async def ensure_allowances(self) -> bool: ...

    async def redeem_positions(self, condition_ids: Sequence[str]) -> Sequence[str]:
        """Redeem resolved positions, returning the condition ids that succeeded.

        A sequence rather than a bare success flag because redemption is
        per-condition -- the venue accepts exactly one id per call and has no batch
        form -- so a partial result is the normal case, and the caller needs to know
        which half it got before deciding what capital is available.
        """
        ...
