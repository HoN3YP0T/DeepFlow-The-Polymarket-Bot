"""Risk approval and exposure accounting.

The test this module exists for is `test_ten_positions_on_one_event_is_one_bet`:
ten independent 2% positions is a diversified book, and ten positions resolving on
the same fixture is one 20% position that every per-position limit waves through.
Everything else here guards the vetoes around it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from deepflow.config.thresholds import RiskLimits
from deepflow.core.domain import Position
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ClobTokenId, ConditionId, EventId, PositionId
from deepflow.risk.exposure import ExposureTracker, MarketRef
from deepflow.risk.limits import MIN_MEANINGFUL_STAKE_USDC, BankrollState, RiskEngine

BANKROLL = Decimal(10_000)
NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _limits(**overrides: object) -> RiskLimits:
    return RiskLimits(**overrides)  # type: ignore[arg-type]


def _ref(n: int, *, event: str | None = "E1", key: str | None = None) -> MarketRef:
    return MarketRef(
        condition_id=ConditionId(f"0x{n:02x}"),
        event_id=EventId(event) if event else None,
        category=MarketCategory.FOOTBALL,
        correlation_key=key,
    )


def _engine(
    *,
    limits: RiskLimits | None = None,
    tracker: ExposureTracker | None = None,
    balance: Decimal = BANKROLL,
    peak: Decimal = BANKROLL,
    realized_today: Decimal = Decimal(0),
) -> tuple[RiskEngine, ExposureTracker]:
    tracker = tracker or ExposureTracker()
    engine = RiskEngine(limits or _limits(), tracker)
    engine.update_bankroll(
        BankrollState(
            balance_usdc=balance,
            peak_balance_usdc=peak,
            realized_pnl_today=realized_today,
        )
    )
    return engine, tracker


def _approve(engine: RiskEngine, ref: MarketRef, *, probability: str = "0.98"):
    return engine.approve(
        probability=Decimal(probability),
        price=Decimal("0.95"),
        uncertainty=Decimal("0.01"),
        condition_id=ref.condition_id,
    )


# --- The disguise ---------------------------------------------------------
def test_ten_positions_on_one_event_is_one_bet() -> None:
    """The failure the correlated limit exists to catch.

    Each stake clears the per-market cap comfortably. Together they are one wager on
    one fixture, and only the correlated-group limit sees it.
    """
    engine, tracker = _engine(
        limits=_limits(
            max_correlated_exposure_fraction=Decimal("0.10"),
            max_event_exposure_fraction=Decimal("1"),
            max_position_fraction=Decimal("0.02"),
            max_open_positions=50,
        )
    )
    refs = [_ref(n, event="E1") for n in range(10)]

    approvals = 0
    for ref in refs:
        tracker.register(ref)
        verdict = _approve(engine, ref)
        if verdict.approved:
            assert verdict.sizing is not None
            tracker.apply(ref=ref, stake_usdc=verdict.sizing.stake_usdc)
            approvals += 1
        else:
            assert "correlated-group cap" in verdict.reason
            break

    assert approvals < 10, "the correlated limit must bind before all ten are taken"
    assert tracker.snapshot.by_correlation_group["event:E1"] <= BANKROLL * Decimal("0.10")


def test_positions_on_separate_events_are_not_pooled() -> None:
    """The mirror image: real diversification must not be penalised."""
    engine, tracker = _engine(limits=_limits(max_open_positions=50))
    for n in range(5):
        ref = _ref(n, event=f"E{n}")
        tracker.register(ref)
        verdict = _approve(engine, ref)
        assert verdict.approved, verdict.reason
        assert verdict.sizing is not None
        tracker.apply(ref=ref, stake_usdc=verdict.sizing.stake_usdc)
    assert len(tracker.snapshot.by_correlation_group) == 5


def test_an_explicit_correlation_key_pools_across_events() -> None:
    """Correlation an event id cannot express: one underlying, two events."""
    tracker = ExposureTracker()
    a = _ref(1, event="E1", key="btc-print-1200")
    b = _ref(2, event="E2", key="btc-print-1200")
    tracker.register(a)
    tracker.register(b)
    assert tracker.correlation_group(a.condition_id) == tracker.correlation_group(b.condition_id)


def test_an_unknown_market_is_its_own_group() -> None:
    """Neither pooled with strangers nor exempt from the limit.

    A shared constant would make the correlated cap bind on a coincidence; no group
    at all would exempt it. Being its own group is wrong in neither direction.
    """
    tracker = ExposureTracker()
    first = tracker.correlation_group(ConditionId("0xaa"))
    second = tracker.correlation_group(ConditionId("0xbb"))
    assert first != second
    assert first.startswith("market:")


# --- The vetoes, in order -------------------------------------------------
def test_daily_loss_limit_stops_trading() -> None:
    engine, _tracker = _engine(
        realized_today=Decimal(-600), limits=_limits(max_daily_loss_fraction=Decimal("0.05"))
    )
    verdict = _approve(engine, _ref(1))
    assert not verdict.approved
    assert "daily loss" in verdict.reason


def test_unrealized_loss_does_not_stop_trading() -> None:
    """An unrealized loss is a position to manage, not a day to stop over."""
    engine, _ = _engine(realized_today=Decimal(0), balance=BANKROLL, peak=BANKROLL)
    assert _approve(engine, _ref(1)).approved


def test_drawdown_limit_stops_trading() -> None:
    engine, _ = _engine(balance=Decimal(8_000), peak=Decimal(10_000))
    verdict = _approve(engine, _ref(1))
    assert not verdict.approved
    assert "drawdown" in verdict.reason


def test_drawdown_is_measured_from_peak_not_from_the_start() -> None:
    """A recovered account is not permanently barred by an old low."""
    engine, _ = _engine(balance=Decimal(10_000), peak=Decimal(10_000))
    assert _approve(engine, _ref(1)).approved


def test_position_count_cap_refuses_before_sizing() -> None:
    tracker = ExposureTracker()
    for n in range(3):
        tracker.apply(ref=_ref(n, event=f"E{n}"), stake_usdc=Decimal(10))
    engine, _ = _engine(tracker=tracker, limits=_limits(max_open_positions=3))
    verdict = _approve(engine, _ref(99, event="E99"))
    assert not verdict.approved
    assert "open positions" in verdict.reason
    assert verdict.sizing is None, "refused before sizing ran"


def test_no_edge_sizes_to_zero_and_is_refused() -> None:
    engine, _ = _engine()
    verdict = _approve(engine, _ref(1), probability="0.90")  # below the 0.95 price
    assert not verdict.approved
    assert "zero" in verdict.reason
    assert verdict.sizing is not None
    assert verdict.sizing.binding_constraint == "no_edge"


def test_a_dust_stake_is_refused_above_the_venue_minimum() -> None:
    """Not "not allowed" but "not worth it".

    An order at the venue's own floor still pays the taker fee, on a position too
    small for its edge to cover it.
    """
    engine, _ = _engine(balance=Decimal(100), peak=Decimal(100))
    verdict = _approve(engine, _ref(1))
    assert not verdict.approved
    assert "below minimum" in verdict.reason
    assert verdict.sizing is not None
    assert verdict.sizing.stake_usdc < MIN_MEANINGFUL_STAKE_USDC


def test_a_rejection_still_reports_the_size_it_would_have_taken() -> None:
    """The stake that would have been taken is the only record of whether a limit
    is binding meaningfully or strangling everything."""
    engine, tracker = _engine(limits=_limits(max_total_exposure_fraction=Decimal("0.001")))
    ref = _ref(1)
    tracker.register(ref)
    verdict = _approve(engine, ref)
    assert not verdict.approved
    assert verdict.sizing is not None
    assert verdict.sizing.stake_usdc > 0


@pytest.mark.parametrize("price", ["0", "1", "1.5"])
def test_a_price_outside_the_unit_interval_is_refused(price: str) -> None:
    engine, _ = _engine()
    verdict = engine.approve(
        probability=Decimal("0.98"),
        price=Decimal(price),
        uncertainty=Decimal("0.01"),
        condition_id=ConditionId("0x01"),
    )
    assert not verdict.approved
    assert "outside" in verdict.reason


# --- would_exceed -------------------------------------------------------
def test_a_non_positive_bankroll_refuses_everything() -> None:
    """Fractional limits against zero capital are meaningless, not satisfied."""
    tracker = ExposureTracker()
    breach = tracker.would_exceed(
        stake_usdc=Decimal(10),
        condition_id=ConditionId("0x01"),
        bankroll=Decimal(0),
        limits=_limits(),
    )
    assert breach is not None
    assert "non-positive" in breach


def test_the_narrowest_true_breach_is_the_one_reported() -> None:
    """ "per-event cap" is more useful in a rejection than "total exposure cap"."""
    tracker = ExposureTracker()
    ref = _ref(1, event="E1")
    tracker.apply(ref=ref, stake_usdc=Decimal(5_000))
    breach = tracker.would_exceed(
        stake_usdc=Decimal(5_000),
        condition_id=ref.condition_id,
        bankroll=BANKROLL,
        limits=_limits(),
    )
    assert breach is not None
    assert breach.startswith("per-market cap")


# --- rebuild ------------------------------------------------------------
class _Positions:
    def __init__(self, positions: list[Position]) -> None:
        self._positions = positions

    async def upsert(self, position: Position) -> None: ...
    async def get(self, position_id: PositionId) -> Position | None: ...
    async def list_open(self) -> list[Position]:
        return self._positions


def _position(n: int, *, shares: str, price: str) -> Position:
    return Position(
        position_id=PositionId(f"p{n}"),
        condition_id=ConditionId(f"0x{n:02x}"),
        token_id=ClobTokenId(str(n)),
        shares=Decimal(shares),
        average_entry_price=Decimal(price),
        entry_probability=Decimal("0.98"),
        opened_at=NOW,
    )


@pytest.mark.asyncio
async def test_rebuild_uses_cost_basis_not_mark_value() -> None:
    """Marking to market would free capacity as a position moved in our favour.

    Concentration would then increase exactly when it feels safest, so exposure is
    what was committed: shares times average entry.
    """
    tracker = ExposureTracker(_Positions([_position(1, shares="100", price="0.90")]))
    snapshot = await tracker.rebuild()
    assert snapshot.total_usdc == Decimal(90)
    assert snapshot.open_position_count == 1


@pytest.mark.asyncio
async def test_rebuild_discards_stale_state_rather_than_preserving_it() -> None:
    """An incrementally maintained counter that drifted is worse than no counter."""
    tracker = ExposureTracker(_Positions([]))
    tracker.apply(ref=_ref(1), stake_usdc=Decimal(500))
    assert tracker.snapshot.total_usdc == Decimal(500)

    snapshot = await tracker.rebuild()
    assert snapshot.total_usdc == Decimal(0)
    assert snapshot.open_position_count == 0


@pytest.mark.asyncio
async def test_rebuild_without_a_repository_clears_rather_than_keeps() -> None:
    """A tracker asked to rebuild from nothing knows nothing.

    An empty snapshot refuses trades through the limits; a stale one authorises them.
    """
    tracker = ExposureTracker()
    tracker.apply(ref=_ref(1), stake_usdc=Decimal(500))
    snapshot = await tracker.rebuild()
    assert snapshot.total_usdc == Decimal(0)


@pytest.mark.asyncio
async def test_rebuild_regroups_by_registered_correlation() -> None:
    tracker = ExposureTracker(
        _Positions(
            [_position(1, shares="100", price="0.90"), _position(2, shares="50", price="0.80")]
        )
    )
    tracker.register(MarketRef(condition_id=ConditionId("0x01"), event_id=EventId("E1")))
    tracker.register(MarketRef(condition_id=ConditionId("0x02"), event_id=EventId("E1")))
    snapshot = await tracker.rebuild()
    assert snapshot.by_correlation_group["event:E1"] == Decimal(130)
