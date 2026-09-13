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
from typing import Final

from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import OrderRecord
from deepflow.core.enums import OrderStatus
from deepflow.core.logging import get_logger
from deepflow.core.types import ClientOrderKey
from deepflow.ports.execution import AccountPort, ExecutionPort
from deepflow.ports.repository import UnitOfWork

log = get_logger(__name__)

#: Statuses proving the order reached the book and did something.
#:
#: ``MATCHED_UNSETTLED`` counts: the trade exists even though settlement has not
#: confirmed, so re-intending would duplicate a position that is already probable.
#: Booking it as final is a separate question, answered by following settlement.
_FILLED_STATES: Final = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.PARTIALLY_FILLED,
        OrderStatus.MATCHED_UNSETTLED,
    }
)

#: Statuses proving the order is finished having filled nothing.
_GONE_STATES: Final = frozenset({OrderStatus.CANCELLED, OrderStatus.REJECTED})


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


class UncertainOutcome(StrEnum):
    """What became of an order whose submission outcome was unknown.

    Four answers, and the distinction between the last two is the one that matters:
    ``ABSENT`` means the venue does not have it and it is safe to re-intend, while
    ``UNRESOLVED`` means we still do not know. Collapsing them is how a retry
    doubles a position.
    """

    FILLED = "FILLED"
    RESTING = "RESTING"
    ABSENT = "ABSENT"
    UNRESOLVED = "UNRESOLVED"


@dataclass(frozen=True, slots=True)
class UncertainResolution:
    """The verdict on one uncertain order, with the evidence behind it."""

    client_key: str
    outcome: UncertainOutcome
    record: OrderRecord | None = None
    detail: str = ""

    @property
    def safe_to_reintend(self) -> bool:
        """Whether the caller may build a fresh intent for this trade.

        **Only** when the venue positively does not have the order. Every other
        answer -- including "we could not tell" -- forbids it, because the cost of
        being wrong is a duplicate position and the cost of waiting is a missed
        trade.
        """
        return self.outcome is UncertainOutcome.ABSENT


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
        clock: Clock | None = None,
    ) -> None:
        self._execution = execution
        self._account = account
        self._uow = uow
        self._clock = clock or SystemClock()

    async def reconcile(self, trigger: ReconcileTrigger) -> ReconciliationReport:
        """Compare balance, open orders and positions against the venue.

        Venue state wins on every conflict. It is the settlement record; the local
        database is a cache of it, and a cache that disagrees with its source is
        wrong by definition. So this method **reports** rather than repairs -- the
        caller trips ``RECONCILIATION_FAILURE`` on a non-empty report, and a human
        or a later repair step decides what to write.

        A failure to *read* the venue is itself a discrepancy, not an early return.
        "We could not check" and "we checked and it matched" must never produce the
        same report, because the second one clears a breaker and the first must not.
        """
        report = ReconciliationReport(trigger=trigger, ran_at=self._clock.now())

        await self._reconcile_balance(report)
        await self._reconcile_orders(report)
        await self._reconcile_positions(report)

        log.info(
            "reconcile.completed",
            trigger=str(trigger),
            succeeded=report.succeeded,
            discrepancies=len(report.discrepancies),
        )
        await self._persist(report)
        return report

    async def _reconcile_balance(self, report: ReconciliationReport) -> None:
        """Record the venue's collateral balance, and any drift from ours.

        Balance is recorded even when it matches: the figure is what the risk engine
        sizes against, and a report that omits it cannot answer "how much did we
        have when this ran" afterwards.
        """
        try:
            report.balance_remote = await self._account.get_collateral_balance()
        except Exception as exc:
            report.discrepancies.append(
                Discrepancy(
                    kind="balance_unreadable",
                    identifier="collateral",
                    local="",
                    remote=f"{type(exc).__name__}: {exc}",
                )
            )

    async def _reconcile_orders(self, report: ReconciliationReport) -> None:
        """Diff locally-unresolved orders against the venue's open orders.

        Two directions, and both matter:

        * **Local says working, venue does not have it.** The order filled, was
          cancelled, or never landed. Either way our view is stale and the position
          may be wrong.
        * **Venue is working an order we have no record of.** Worse, and the reason
          this direction is checked at all: an orphan at the venue is a live
          commitment nobody is managing, and it will not appear in any local
          exposure figure. A process killed mid-submission leaves exactly this.
        """
        try:
            remote_orders = await self._execution.list_open_orders()
        except Exception as exc:
            report.discrepancies.append(
                Discrepancy(
                    kind="orders_unreadable",
                    identifier="open_orders",
                    local="",
                    remote=f"{type(exc).__name__}: {exc}",
                )
            )
            return

        remote_by_key = {str(order.client_key): order for order in remote_orders}
        local_unresolved = await self._uow.orders.list_unresolved()

        for local in local_unresolved:
            key = str(local.client_key)
            remote = remote_by_key.pop(key, None)
            if remote is None:
                report.discrepancies.append(
                    Discrepancy(
                        kind="order_missing_at_venue",
                        identifier=key,
                        local=str(local.status),
                        remote="absent",
                    )
                )
            elif remote.status is not local.status or remote.filled_shares != local.filled_shares:
                report.discrepancies.append(
                    Discrepancy(
                        kind="order_state_differs",
                        identifier=key,
                        local=f"{local.status} filled={local.filled_shares}",
                        remote=f"{remote.status} filled={remote.filled_shares}",
                    )
                )

        for key, orphan in remote_by_key.items():
            report.discrepancies.append(
                Discrepancy(
                    kind="orphan_order_at_venue",
                    identifier=key,
                    local="absent",
                    remote=f"{orphan.status} filled={orphan.filled_shares}",
                )
            )

    async def _reconcile_positions(self, report: ReconciliationReport) -> None:
        """Diff venue positions against local ones, by token.

        Compared on **shares**, not value: a position's mark moves constantly and a
        value comparison would report a discrepancy on every price tick. Share count
        is the thing that either matches or does not.
        """
        try:
            remote_positions = await self._account.list_positions()
        except Exception as exc:
            report.discrepancies.append(
                Discrepancy(
                    kind="positions_unreadable",
                    identifier="positions",
                    local="",
                    remote=f"{type(exc).__name__}: {exc}",
                )
            )
            return

        remote_shares: dict[str, Decimal] = {}
        for position in remote_positions:
            token = str(position.token_id)
            remote_shares[token] = remote_shares.get(token, Decimal(0)) + position.shares

        local_shares: dict[str, Decimal] = {}
        for position in await self._uow.positions.list_open():
            token = str(position.token_id)
            local_shares[token] = local_shares.get(token, Decimal(0)) + position.shares

        for token in sorted(set(remote_shares) | set(local_shares)):
            local = local_shares.get(token, Decimal(0))
            remote = remote_shares.get(token, Decimal(0))
            if local != remote:
                report.discrepancies.append(
                    Discrepancy(
                        kind="position_shares_differ",
                        identifier=token,
                        local=str(local),
                        remote=str(remote),
                    )
                )

    async def _persist(self, report: ReconciliationReport) -> None:
        """Journal the report, successful or not.

        A clean reconciliation is evidence too -- it is what dates the last moment
        the system is known to have agreed with the venue. Recorded through the
        journal rather than a table of its own so it sits in the same ordered stream
        as the decisions around it.

        A persistence failure is logged and swallowed. Reconciliation's output is the
        returned report, which the caller acts on; losing the row costs history, and
        raising here would turn a database hiccup into a failure to establish ground
        truth.
        """
        try:
            await self._uow.journal.record_decision(
                {
                    "kind": "RECONCILIATION",
                    "reason": (
                        "matched"
                        if report.succeeded
                        else f"{len(report.discrepancies)} discrepancy(ies)"
                    ),
                    "trigger": str(report.trigger),
                    "balance_remote": report.balance_remote,
                    "discrepancies": [
                        {
                            "kind": d.kind,
                            "identifier": d.identifier,
                            "local": d.local,
                            "remote": d.remote,
                        }
                        for d in report.discrepancies
                    ],
                }
            )
            await self._uow.commit()
        except Exception:
            log.warning("reconcile.persist_failed", exc_info=True)

    async def resolve_uncertain_order(self, client_key: str) -> UncertainResolution:
        """Determine whether an uncertain submission actually executed.

        The single most important routine in the execution path. It runs before
        any retry, because the alternative -- retrying an order that did land --
        doubles the position at a worse price, in a market that has already
        moved against the original.

        The asymmetry is deliberate and total: **only a positive "the venue does not
        have this" permits a re-intend.** A lookup that fails, returns nothing
        conclusive, or raises leaves the outcome ``UNRESOLVED``, which forbids the
        retry. Being wrong in that direction costs a missed trade; being wrong in the
        other costs twice the intended position.

        Note what this does *not* do: it does not cancel, retry, or write a position.
        It answers one question and lets the caller act, because an answer that also
        acts cannot be reused by reconciliation, the order manager and a human
        operator alike.
        """
        try:
            found = await self._execution.find_by_client_key(ClientOrderKey(client_key))
        except Exception as exc:
            log.error("reconcile.uncertain_lookup_failed", client_key=client_key, detail=str(exc))
            return UncertainResolution(
                client_key=client_key,
                outcome=UncertainOutcome.UNRESOLVED,
                detail=f"lookup failed: {type(exc).__name__}: {exc}",
            )

        if found is None:
            # The venue positively does not have it. The only answer that permits a
            # fresh intent.
            log.info("reconcile.uncertain_absent", client_key=client_key)
            return UncertainResolution(
                client_key=client_key,
                outcome=UncertainOutcome.ABSENT,
                detail="venue has no order under this key",
            )

        if found.status in _FILLED_STATES:
            log.info(
                "reconcile.uncertain_filled",
                client_key=client_key,
                status=str(found.status),
                filled=str(found.filled_shares),
            )
            return UncertainResolution(
                client_key=client_key,
                outcome=UncertainOutcome.FILLED,
                record=found,
                detail=f"{found.status} filled={found.filled_shares}",
            )

        if found.status in _GONE_STATES:
            # Cancelled or rejected: the order existed and is finished, having done
            # nothing. Not ABSENT -- the key is spent, so a re-intend needs a new one.
            return UncertainResolution(
                client_key=client_key,
                outcome=UncertainOutcome.UNRESOLVED,
                record=found,
                detail=f"terminal without fill: {found.status}; key is spent",
            )

        return UncertainResolution(
            client_key=client_key,
            outcome=UncertainOutcome.RESTING,
            record=found,
            detail=f"live at the venue: {found.status}",
        )
