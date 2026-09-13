"""Reconciliation, and the one asymmetry the execution path depends on.

`resolve_uncertain_order` answers "did that order actually land?". Only a positive
*no* permits a retry. Every other answer — including "the lookup failed" — forbids
it, because being wrong that way costs a missed trade while being wrong the other
way costs twice the intended position.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from deepflow.core.clock import ManualClock
from deepflow.core.domain import OrderIntent, OrderRecord, Position
from deepflow.core.enums import OrderSide, OrderStatus, OrderType
from deepflow.core.types import (
    ClientOrderKey,
    ClobTokenId,
    ConditionId,
    OrderId,
    PositionId,
)
from deepflow.execution.reconciliation import (
    Reconciler,
    ReconcileTrigger,
    UncertainOutcome,
)

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
KEY = "k1"

# The venue indexes no client key (finding 66), so a lookup is by intent: the
# fingerprint the key is itself derived from.
INTENT = OrderIntent(
    client_key=ClientOrderKey(KEY),
    condition_id=ConditionId("0xabc"),
    token_id=ClobTokenId("1"),
    side=OrderSide.BUY,
    order_type=OrderType.LIMIT,
    size_shares=Decimal(100),
    limit_price=Decimal("0.95"),
    max_slippage_bps=Decimal(50),
)


def _order(status: OrderStatus, *, key: str = KEY, filled: str = "0") -> OrderRecord:
    return OrderRecord(
        client_key=ClientOrderKey(key),
        order_id=OrderId("o1"),
        status=status,
        filled_shares=Decimal(filled),
    )


def _position(*, token: str = "1", shares: str = "100") -> Position:
    return Position(
        position_id=PositionId(f"p-{token}"),
        condition_id=ConditionId("0xabc"),
        token_id=ClobTokenId(token),
        shares=Decimal(shares),
        average_entry_price=Decimal("0.95"),
        entry_probability=Decimal("0.98"),
        opened_at=NOW,
    )


class _Venue:
    def __init__(
        self,
        *,
        found: OrderRecord | None = None,
        open_orders: list[OrderRecord] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._found = found
        self._open = open_orders or []
        self._raises = raises

    async def submit(self, intent: object) -> OrderRecord: ...
    async def cancel(self, order_id: OrderId) -> OrderRecord: ...
    async def cancel_all(self) -> list[OrderRecord]:
        return []

    async def get_order(self, order_id: OrderId) -> OrderRecord | None:
        return None

    async def find_by_intent(self, intent: OrderIntent) -> OrderRecord | None:
        if self._raises:
            raise self._raises
        return self._found

    async def list_open_orders(self) -> list[OrderRecord]:
        if self._raises:
            raise self._raises
        return self._open


class _Account:
    def __init__(
        self,
        *,
        balance: Decimal = Decimal(1000),
        positions: list[Position] | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._balance = balance
        self._positions = positions or []
        self._raises = raises

    async def get_collateral_balance(self) -> Decimal:
        if self._raises:
            raise self._raises
        return self._balance

    async def list_positions(self) -> list[Position]:
        if self._raises:
            raise self._raises
        return self._positions


class _Repo:
    def __init__(
        self,
        *,
        unresolved: list[OrderRecord] | None = None,
        positions: list[Position] | None = None,
    ) -> None:
        self.unresolved = unresolved or []
        self.open_positions = positions or []
        self.decisions: list[dict[str, object]] = []

    # order repo
    async def record(self, order: OrderRecord, **kwargs: object) -> None: ...
    async def get_by_client_key(self, key: ClientOrderKey) -> OrderRecord | None:
        return None

    async def list_unresolved(self) -> list[OrderRecord]:
        return self.unresolved

    # position repo
    async def upsert(self, position: Position) -> None: ...
    async def get(self, position_id: PositionId) -> Position | None:
        return None

    async def list_open(self) -> list[Position]:
        return self.open_positions

    # journal
    async def record_signal(self, signal: object) -> None: ...
    async def record_decision(self, entry: dict[str, object]) -> None:
        self.decisions.append(entry)

    async def list_recent(self, *, limit: int = 100) -> list[dict[str, object]]:
        return self.decisions


class _Uow:
    def __init__(self, repo: _Repo) -> None:
        self.orders = repo
        self.positions = repo
        self.journal = repo
        self.markets = repo
        self.snapshots = repo
        self.commits = 0

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *exc: object) -> None: ...
    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None: ...


def _reconciler(venue: _Venue, account: _Account, repo: _Repo) -> Reconciler:
    return Reconciler(
        execution=venue,  # type: ignore[arg-type]
        account=account,  # type: ignore[arg-type]
        uow=_Uow(repo),  # type: ignore[arg-type]
        clock=ManualClock(NOW),
    )


# --- The asymmetry --------------------------------------------------------
@pytest.mark.asyncio
async def test_only_a_positive_absence_permits_a_retry() -> None:
    """The venue does not have it, so a fresh intent cannot duplicate anything."""
    resolution = await _reconciler(_Venue(found=None), _Account(), _Repo()).resolve_uncertain_order(
        INTENT
    )
    assert resolution.outcome is UncertainOutcome.ABSENT
    assert resolution.safe_to_reintend


@pytest.mark.asyncio
async def test_a_failed_lookup_forbids_a_retry() -> None:
    """ "We could not tell" must not read as "it is not there".

    This is the case that costs a duplicate position if it is collapsed into
    ABSENT, and only a missed trade if it is not.
    """
    venue = _Venue(raises=RuntimeError("gateway timeout"))
    resolution = await _reconciler(venue, _Account(), _Repo()).resolve_uncertain_order(INTENT)
    assert resolution.outcome is UncertainOutcome.UNRESOLVED
    assert not resolution.safe_to_reintend
    assert "lookup failed" in resolution.detail


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status",
    [OrderStatus.FILLED, OrderStatus.PARTIALLY_FILLED, OrderStatus.MATCHED_UNSETTLED],
)
async def test_an_order_that_did_something_forbids_a_retry(status: OrderStatus) -> None:
    """MATCHED_UNSETTLED counts: the trade exists even before settlement confirms,
    so re-intending would duplicate a position that is already probable."""
    venue = _Venue(found=_order(status, filled="40"))
    resolution = await _reconciler(venue, _Account(), _Repo()).resolve_uncertain_order(INTENT)
    assert resolution.outcome is UncertainOutcome.FILLED
    assert not resolution.safe_to_reintend
    assert resolution.record is not None


@pytest.mark.asyncio
async def test_a_live_order_is_resting_not_absent() -> None:
    venue = _Venue(found=_order(OrderStatus.OPEN))
    resolution = await _reconciler(venue, _Account(), _Repo()).resolve_uncertain_order(INTENT)
    assert resolution.outcome is UncertainOutcome.RESTING
    assert not resolution.safe_to_reintend


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [OrderStatus.CANCELLED, OrderStatus.REJECTED])
async def test_a_spent_key_is_not_reported_as_absent(status: OrderStatus) -> None:
    """The order existed and is finished having done nothing — but the key is spent,
    so a re-intend needs a new one and must not reuse this."""
    venue = _Venue(found=_order(status))
    resolution = await _reconciler(venue, _Account(), _Repo()).resolve_uncertain_order(INTENT)
    assert resolution.outcome is UncertainOutcome.UNRESOLVED
    assert not resolution.safe_to_reintend
    assert "spent" in resolution.detail


@pytest.mark.asyncio
async def test_resolution_does_not_act() -> None:
    """It answers one question. An answer that also acts cannot be reused by the
    order manager, reconciliation and a human operator alike."""
    repo = _Repo()
    venue = _Venue(found=_order(OrderStatus.FILLED, filled="100"))
    await _reconciler(venue, _Account(), repo).resolve_uncertain_order(INTENT)
    assert repo.decisions == []


# --- Full reconciliation --------------------------------------------------
@pytest.mark.asyncio
async def test_a_clean_reconciliation_succeeds_and_is_still_recorded() -> None:
    """A match is evidence too: it dates the last moment we are known to have
    agreed with the venue."""
    repo = _Repo()
    report = await _reconciler(_Venue(), _Account(), repo).reconcile(ReconcileTrigger.STARTUP)
    assert report.succeeded
    assert report.balance_remote == Decimal(1000)
    assert repo.decisions and repo.decisions[0]["reason"] == "matched"


@pytest.mark.asyncio
async def test_an_unreadable_venue_is_a_discrepancy_not_a_pass() -> None:
    """ "We could not check" and "we checked and it matched" must not produce the
    same report — the second clears a breaker and the first must not."""
    account = _Account(raises=RuntimeError("503"))
    venue = _Venue(raises=RuntimeError("503"))
    report = await _reconciler(venue, account, _Repo()).reconcile(ReconcileTrigger.PERIODIC)
    assert not report.succeeded
    kinds = {d.kind for d in report.discrepancies}
    assert kinds == {"balance_unreadable", "orders_unreadable", "positions_unreadable"}


@pytest.mark.asyncio
async def test_a_locally_working_order_absent_at_the_venue_is_flagged() -> None:
    repo = _Repo(unresolved=[_order(OrderStatus.OPEN)])
    report = await _reconciler(_Venue(open_orders=[]), _Account(), repo).reconcile(
        ReconcileTrigger.RECONNECT
    )
    assert [d.kind for d in report.discrepancies] == ["order_missing_at_venue"]


@pytest.mark.asyncio
async def test_an_orphan_order_at_the_venue_is_flagged() -> None:
    """The direction that matters most: a live commitment nobody is managing, which
    appears in no local exposure figure. A process killed mid-submission leaves
    exactly this."""
    venue = _Venue(open_orders=[_order(OrderStatus.OPEN, key="ghost")])
    report = await _reconciler(venue, _Account(), _Repo()).reconcile(ReconcileTrigger.RESTART)
    assert [d.kind for d in report.discrepancies] == ["orphan_order_at_venue"]
    assert report.discrepancies[0].identifier == "ghost"


@pytest.mark.asyncio
async def test_a_differing_fill_quantity_is_flagged() -> None:
    repo = _Repo(unresolved=[_order(OrderStatus.PARTIALLY_FILLED, filled="10")])
    venue = _Venue(open_orders=[_order(OrderStatus.PARTIALLY_FILLED, filled="60")])
    report = await _reconciler(venue, _Account(), repo).reconcile(ReconcileTrigger.PERIODIC)
    assert [d.kind for d in report.discrepancies] == ["order_state_differs"]
    assert "60" in report.discrepancies[0].remote


@pytest.mark.asyncio
async def test_positions_are_compared_on_shares_not_value() -> None:
    """A value comparison would report a discrepancy on every price tick."""
    repo = _Repo(positions=[_position(shares="100")])
    account = _Account(positions=[_position(shares="100")])
    report = await _reconciler(_Venue(), account, repo).reconcile(ReconcileTrigger.PERIODIC)
    assert report.succeeded


@pytest.mark.asyncio
async def test_a_position_the_venue_does_not_have_is_flagged() -> None:
    repo = _Repo(positions=[_position(token="7", shares="100")])
    report = await _reconciler(_Venue(), _Account(), repo).reconcile(ReconcileTrigger.STARTUP)
    assert [d.kind for d in report.discrepancies] == ["position_shares_differ"]
    assert report.discrepancies[0].identifier == "7"


@pytest.mark.asyncio
async def test_a_position_only_the_venue_has_is_flagged() -> None:
    """A phantom position in the other direction: real capital we do not know about."""
    account = _Account(positions=[_position(token="9", shares="50")])
    report = await _reconciler(_Venue(), account, _Repo()).reconcile(ReconcileTrigger.STARTUP)
    assert [d.kind for d in report.discrepancies] == ["position_shares_differ"]
    assert report.discrepancies[0].local == "0"


@pytest.mark.asyncio
async def test_positions_on_one_token_are_summed_before_comparing() -> None:
    """Two local lots against one venue position is agreement, not a discrepancy."""
    repo = _Repo(positions=[_position(shares="40"), _position(shares="60")])
    account = _Account(positions=[_position(shares="100")])
    report = await _reconciler(_Venue(), account, repo).reconcile(ReconcileTrigger.PERIODIC)
    assert report.succeeded


@pytest.mark.asyncio
async def test_every_discrepancy_is_reported_not_just_the_first() -> None:
    """The count is what distinguishes one stale row from a systemically wrong view."""
    repo = _Repo(unresolved=[_order(OrderStatus.OPEN)], positions=[_position(token="7")])
    venue = _Venue(open_orders=[_order(OrderStatus.OPEN, key="ghost")])
    report = await _reconciler(venue, _Account(), repo).reconcile(ReconcileTrigger.MANUAL)
    assert len(report.discrepancies) == 3


@pytest.mark.asyncio
async def test_a_persistence_failure_does_not_hide_the_report() -> None:
    """The returned report is reconciliation's output; losing the row costs history,
    and raising would turn a database hiccup into a failure to establish truth."""

    class _FailingRepo(_Repo):
        async def record_decision(self, entry: dict[str, object]) -> None:
            raise RuntimeError("database down")

    report = await _reconciler(_Venue(), _Account(), _FailingRepo()).reconcile(
        ReconcileTrigger.STARTUP
    )
    assert report.succeeded
