"""Account reconciliation. Section 20.

Compares venue state against the local database on startup, on reconnect,
periodically, and whenever an execution outcome is uncertain.

On failure the rule is unambiguous: HALT NEW TRADING. A system that does not
know its own position size cannot size the next trade, cannot compute exposure,
and cannot tell a fill from a phantom. Continuing to trade through a
reconciliation failure compounds an unknown into a larger unknown.

Exits remain permitted, because reducing an uncertain position is the action
that shrinks the problem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from deepflow.core.logging import get_logger
from deepflow.ports.execution import AccountPort, ExecutionPort
from deepflow.ports.repository import UnitOfWork

log = get_logger(__name__)


class ReconcileTrigger(StrEnum):
    STARTUP = "STARTUP"
    RESTART = "RESTART"
    RECONNECT = "RECONNECT"
    EXECUTION_UNCERTAIN = "EXECUTION_UNCERTAIN"
    PERIODIC = "PERIODIC"
    MANUAL = "MANUAL"


@dataclass(frozen=True, slots=True)
class Discrepancy:
    kind: str
    identifier: str
    local: str
    remote: str


@dataclass(slots=True)
class ReconciliationReport:
    trigger: ReconcileTrigger
    ran_at: datetime
    discrepancies: list[Discrepancy] = field(default_factory=list)
    balance_local: Decimal | None = None
    balance_remote: Decimal | None = None

    @property
    def succeeded(self) -> bool:
        return not self.discrepancies


class Reconciler:
    """Establishes ground truth against the venue."""

    def __init__(
        self,
        *,
        execution: ExecutionPort,
        account: AccountPort,
        uow: UnitOfWork,
    ) -> None:
        self._execution = execution
        self._account = account
        self._uow = uow

    async def reconcile(self, trigger: ReconcileTrigger) -> ReconciliationReport:
        """Compare balance, open orders, fills, positions and P&L.

        TODO(skeleton):
        1. fetch venue balance, open orders and positions
        2. load local equivalents
        3. diff, recording each mismatch as a :class:`Discrepancy`
        4. venue state wins on conflict -- it is the settlement record; local
           state is a cache of it
        5. persist the report

        The caller trips ``RECONCILIATION_FAILURE`` on a non-empty report.
        Reconciliation reports; it does not decide.
        """
        raise NotImplementedError("Reconciler.reconcile")

    async def resolve_uncertain_order(self, client_key: str) -> object:
        """Determine whether an uncertain submission actually executed.

        The single most important routine in the execution path. It runs before
        any retry, because the alternative -- retrying an order that did land --
        doubles the position at a worse price, in a market that has already
        moved against the original.
        """
        raise NotImplementedError("Reconciler.resolve_uncertain_order")
