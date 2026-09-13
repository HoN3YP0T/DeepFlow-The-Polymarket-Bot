"""The decision log.

Rejections are recorded with the same care as entries, which is the part that is
easy to skip and expensive to have skipped: the trades taken are a biased sample of
the opportunities seen, so without the rejected set there is no way to tell a gate
that is correctly protective from one that is never satisfied.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from deepflow.core.clock import ManualClock
from deepflow.core.domain import (
    CostBreakdown,
    EvAssessment,
    ExitDecision,
    ProbabilityEstimate,
    Signal,
)
from deepflow.core.enums import ExitAction, MarketCategory, RunMode, SignalAction
from deepflow.core.types import ClobTokenId, ConditionId, PositionId, SignalId
from deepflow.journal.recorder import JournalKind, JournalRecorder
from deepflow.risk.safety_gate import CheckId, CheckResult, GateDecision

NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
YES = ClobTokenId("1")
CID = ConditionId("0xabc")


class _Repo:
    """Captures what was written, and can be told to fail."""

    def __init__(self, *, fail: bool = False) -> None:
        self.entries: list[dict[str, Any]] = []
        self._fail = fail

    async def record_signal(self, signal: Signal) -> None: ...

    async def record_decision(self, entry: dict[str, Any]) -> None:
        if self._fail:
            raise RuntimeError("database unavailable")
        self.entries.append(entry)

    async def list_recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return self.entries[-limit:]


def _signal() -> Signal:
    return Signal(
        signal_id=SignalId("s1"),
        condition_id=CID,
        token_id=YES,
        action=SignalAction.BUY,
        category=MarketCategory.FOOTBALL,
        probability=ProbabilityEstimate(
            token_id=YES,
            model_probability=Decimal("0.97"),
            calibrated_probability=Decimal("0.98"),
            uncertainty=Decimal("0.01"),
            engine="football-v1",
            inputs={"score": "1-0", "minute": "78"},
        ),
        ev=EvAssessment(
            token_id=YES,
            market_probability=Decimal("0.95"),
            model_probability=Decimal("0.98"),
            edge=Decimal("0.03"),
            costs=CostBreakdown(
                fee_bps=Decimal(20),
                slippage_bps=Decimal(5),
                uncertainty_buffer_bps=Decimal(10),
            ),
            fill_probability=Decimal(1),
            net_ev=Decimal("0.0267"),
            confidence=85,
        ),
        target_price=Decimal("0.95"),
        generated_at=NOW,
        rationale="two-goal lead, 78th minute",
    )


def _gate(*, approved: bool) -> GateDecision:
    results = [
        CheckResult(check=CheckId.MARKET_VALID, passed=True, detail="ok"),
        CheckResult(
            check=CheckId.POSITIVE_NET_EV,
            passed=approved,
            detail="" if approved else "net_ev=-0.004",
        ),
        CheckResult(check=CheckId.DATA_FRESH, passed=True, detail="age 0.4s"),
    ]
    return GateDecision(approved=approved, results=tuple(results))


def _recorder(repo: _Repo, mode: RunMode = RunMode.PAPER) -> JournalRecorder:
    return JournalRecorder(repository=repo, clock=ManualClock(NOW), mode=mode)


@pytest.mark.asyncio
async def test_an_entry_records_enough_to_reconstruct_the_decision() -> None:
    """The live state that produced a decision will be gone; the row is what is left."""
    repo = _Repo()
    await _recorder(repo).record_entry(_signal(), gate=_gate(approved=True))

    entry = repo.entries[0]
    assert entry["kind"] == JournalKind.ENTERED
    assert entry["reason"] == "two-goal lead, 78th minute"
    for field in (
        "market_probability",
        "calibrated_probability",
        "edge",
        "net_ev",
        "ev_confidence",
        "engine",
        "model_inputs",
    ):
        assert field in entry, field


@pytest.mark.asyncio
async def test_a_rejection_is_recorded_as_fully_as_an_entry() -> None:
    """The biased-sample problem: without this, the gates cannot be calibrated."""
    repo = _Repo()
    recorder = _recorder(repo)
    await recorder.record_entry(_signal(), gate=_gate(approved=True))
    await recorder.record_rejection(_signal(), gate=_gate(approved=False))

    entered, rejected = repo.entries
    assert rejected["kind"] == JournalKind.REJECTED
    # Same evidence, different verdict -- not a thinner row.
    evidence = {"edge", "net_ev", "cost_total_bps", "calibrated_probability", "gate_results"}
    assert evidence <= set(entered)
    assert evidence <= set(rejected)


@pytest.mark.asyncio
async def test_a_rejection_names_the_failing_check() -> None:
    repo = _Repo()
    await _recorder(repo).record_rejection(_signal(), gate=_gate(approved=False))
    entry = repo.entries[0]
    assert "net_ev=-0.004" in entry["reason"]
    assert str(CheckId.POSITIVE_NET_EV) in entry["gate_blocking"]


@pytest.mark.asyncio
async def test_every_check_is_recorded_not_only_the_failures() -> None:
    """ "Barely cleared" and "cleared comfortably" are different, and a failure list
    cannot express either."""
    repo = _Repo()
    await _recorder(repo).record_entry(_signal(), gate=_gate(approved=True))
    results = repo.entries[0]["gate_results"]
    assert set(results) == {
        str(CheckId.MARKET_VALID),
        str(CheckId.POSITIVE_NET_EV),
        str(CheckId.DATA_FRESH),
    }
    assert results[str(CheckId.DATA_FRESH)]["detail"] == "age 0.4s"


@pytest.mark.asyncio
async def test_cost_terms_are_recorded_individually() -> None:
    """A rejection on cost is only actionable if you can see which term dominated."""
    repo = _Repo()
    await _recorder(repo).record_rejection(_signal(), gate=_gate(approved=False))
    entry = repo.entries[0]
    assert entry["cost_fee_bps"] == Decimal(20)
    assert entry["cost_slippage_bps"] == Decimal(5)
    assert entry["cost_uncertainty_bps"] == Decimal(10)
    assert entry["cost_total_bps"] == Decimal(35)


@pytest.mark.asyncio
async def test_both_probabilities_are_recorded() -> None:
    """The gap between raw and calibrated is the only evidence, in flight, that
    calibration is doing anything at all."""
    repo = _Repo()
    await _recorder(repo).record_entry(_signal(), gate=_gate(approved=True))
    entry = repo.entries[0]
    assert entry["model_probability"] == Decimal("0.97")
    assert entry["calibrated_probability"] == Decimal("0.98")


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [RunMode.PAPER, RunMode.SHADOW, RunMode.LIVE])
async def test_run_mode_is_on_every_row(mode: RunMode) -> None:
    """A PAPER decision and a LIVE one are indistinguishable afterwards otherwise,
    and comparing them is the entire point of the promotion path."""
    repo = _Repo()
    recorder = _recorder(repo, mode)
    await recorder.record_entry(_signal(), gate=_gate(approved=True))
    await recorder.record_rejection(_signal(), gate=_gate(approved=False))
    await recorder.record_hold(_exit(ExitAction.HOLD))
    await recorder.record_exit(_exit(ExitAction.FULL_EXIT), outcome={"filled_fraction": "1.0"})
    assert all(entry["run_mode"] == str(mode) for entry in repo.entries)
    assert len(repo.entries) == 4


def _exit(action: ExitAction) -> ExitDecision:
    return ExitDecision(
        position_id=PositionId("p1"),
        action=action,
        exit_score=80,
        fraction=Decimal("1") if action is ExitAction.FULL_EXIT else Decimal(0),
        reason="resolution risk rising",
        triggers=("late_goal_window", "spread_widening"),
    )


@pytest.mark.asyncio
async def test_a_hold_is_recorded_as_a_decision() -> None:
    """An unrecorded hold is indistinguishable afterwards from never having looked."""
    repo = _Repo()
    await _recorder(repo).record_hold(_exit(ExitAction.HOLD))
    entry = repo.entries[0]
    assert entry["kind"] == JournalKind.HELD
    assert entry["exit_score"] == 80
    assert entry["triggers"] == ["late_goal_window", "spread_widening"]


@pytest.mark.asyncio
async def test_an_exit_records_the_outcome_separately_from_the_intent() -> None:
    """Intended fraction and filled fraction are different numbers, and a journal
    that keeps only the intent cannot show a partial exit at all."""
    repo = _Repo()
    await _recorder(repo).record_exit(
        _exit(ExitAction.FULL_EXIT), outcome={"filled_fraction": "0.4", "reason": "book thinned"}
    )
    entry = repo.entries[0]
    assert entry["fraction"] == Decimal("1")
    assert entry["outcome"]["filled_fraction"] == "0.4"


@pytest.mark.asyncio
async def test_a_write_failure_is_logged_and_never_raised() -> None:
    """The journal is a record, not a safety mechanism -- the gate is.

    Losing a row costs analysis; letting the insert propagate could abandon an exit
    halfway. Both are worse than a warning, and a silent loss is worse than all
    three.
    """
    repo = _Repo(fail=True)
    recorder = _recorder(repo)
    await recorder.record_entry(_signal(), gate=_gate(approved=True))
    await recorder.record_exit(_exit(ExitAction.FULL_EXIT), outcome={})
    assert repo.entries == []
