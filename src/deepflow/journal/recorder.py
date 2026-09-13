"""Trade explanation and journal. Section 23.

Every decision records why: WHY ENTERED, WHY REJECTED, WHY HELD, WHY EXITED.

Rejections are recorded as carefully as entries, and this is the part that is
easy to skip and expensive to have skipped. The trades taken are a biased
sample of the opportunities seen; without the rejected set there is no way to
tell a gate that is correctly protective from one that is simply never
satisfied, and no way to know which threshold to move.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from deepflow.core.clock import Clock
from deepflow.core.domain import ExitDecision, Signal
from deepflow.core.enums import RunMode
from deepflow.core.logging import get_logger
from deepflow.ports.repository import JournalRepository
from deepflow.risk.safety_gate import GateDecision

log = get_logger(__name__)


class JournalKind(StrEnum):
    ENTERED = "ENTERED"
    REJECTED = "REJECTED"
    HELD = "HELD"
    EXITED = "EXITED"


class JournalRecorder:
    """Writes the decision log.

    Every method swallows a write failure and logs it loudly rather than raising.
    That is a considered trade: the journal is a record, not a safety mechanism --
    the gate is the safety mechanism -- so losing a row costs analysis, while
    letting a failed insert propagate could abandon an exit or a cancel halfway.
    A silent loss would be worse than either, which is why the failure is logged at
    warning with the kind and the reason attached.
    """

    def __init__(self, *, repository: JournalRepository, clock: Clock, mode: RunMode) -> None:
        self._repository = repository
        self._clock = clock
        self._mode = mode

    async def record_entry(
        self, signal: Signal, *, gate: GateDecision, context: dict[str, Any] | None = None
    ) -> None:
        """Record an entry with its full supporting evidence.

        Captures market and model probability, edge, net EV, game/event state,
        flow, smart-money signal, liquidity, risk sizing and execution detail --
        enough to reconstruct the decision later without the live state that
        produced it, which will be gone.
        """
        await self._write(
            JournalKind.ENTERED,
            reason=signal.rationale or "entered",
            signal=signal,
            gate=gate,
            context=context,
        )

    async def record_rejection(
        self, signal: Signal, *, gate: GateDecision, context: dict[str, Any] | None = None
    ) -> None:
        """Record a rejected opportunity and the failing checks.

        Recorded with the same detail as an entry, deliberately. The trades taken
        are a biased sample of the opportunities seen: without the rejected set
        there is no way to tell a gate that is correctly protective from one that is
        never satisfied, and no way to know which threshold to move. A rejection log
        that keeps only the verdict and drops the evidence answers "how many" and
        never "which one, and by how much".
        """
        await self._write(
            JournalKind.REJECTED,
            reason=gate.reason,
            signal=signal,
            gate=gate,
            context=context,
        )

    async def record_hold(self, decision: ExitDecision) -> None:
        """Record a deliberate decision not to act on an open position.

        A hold is a decision, and an unrecorded one is indistinguishable afterwards
        from never having looked. That difference is the whole of a post-mortem on a
        position that should have been closed.
        """
        await self._write(
            JournalKind.HELD,
            reason=decision.reason or f"hold (score {decision.exit_score})",
            exit_decision=decision,
        )

    async def record_exit(self, decision: ExitDecision, *, outcome: dict[str, Any]) -> None:
        """Record an exit and what it actually achieved.

        ``outcome`` is separate from the decision because the two can disagree --
        the intended fraction and the filled fraction are different numbers, and a
        journal that records only the intent cannot show a partial exit at all.
        """
        await self._write(
            JournalKind.EXITED,
            reason=decision.reason or f"exit ({decision.action.value})",
            exit_decision=decision,
            context={"outcome": outcome},
        )

    # --- Internals --------------------------------------------------------
    async def _write(
        self,
        kind: JournalKind,
        *,
        reason: str,
        signal: Signal | None = None,
        gate: GateDecision | None = None,
        exit_decision: ExitDecision | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "kind": str(kind),
            "reason": reason,
            # On every row without exception. A PAPER decision and a LIVE decision
            # look identical in a post-mortem otherwise, and the whole point of the
            # promotion path is being able to compare them.
            "run_mode": str(self._mode),
            "recorded_at": self._clock.now(),
        }
        if signal is not None:
            entry.update(self._signal_fields(signal))
        if gate is not None:
            entry.update(self._gate_fields(gate))
        if exit_decision is not None:
            entry.update(self._exit_fields(exit_decision))
        if context:
            entry.update(context)

        try:
            await self._repository.record_decision(entry)
        except Exception:
            # See the class docstring: loud, never fatal, never silent.
            log.warning("journal.write_failed", kind=str(kind), reason=reason, exc_info=True)

    @staticmethod
    def _signal_fields(signal: Signal) -> dict[str, Any]:
        ev = signal.ev
        estimate = signal.probability
        return {
            "signal_id": str(signal.signal_id),
            "condition_id": str(signal.condition_id),
            "token_id": str(signal.token_id),
            "action": str(signal.action),
            "category": str(signal.category),
            "target_price": signal.target_price,
            "generated_at": signal.generated_at,
            # Both probabilities, not just the calibrated one: the gap between raw
            # and calibrated output is the only in-flight evidence of whether
            # calibration is doing anything.
            "model_probability": estimate.model_probability,
            "calibrated_probability": estimate.calibrated_probability,
            "uncertainty": estimate.uncertainty,
            "engine": estimate.engine,
            "model_inputs": dict(estimate.inputs),
            "market_probability": ev.market_probability,
            "edge": ev.edge,
            "net_ev": ev.net_ev,
            "fill_probability": ev.fill_probability,
            "ev_confidence": ev.confidence,
            # Cost terms individually, not just the total. A rejection on cost is
            # only actionable if you can see which term dominated it.
            "cost_fee_bps": ev.costs.fee_bps,
            "cost_spread_bps": ev.costs.spread_cost_bps,
            "cost_slippage_bps": ev.costs.slippage_bps,
            "cost_uncertainty_bps": ev.costs.uncertainty_buffer_bps,
            "cost_total_bps": ev.costs.total_bps,
            "smart_money": None if signal.smart_money is None else str(signal.smart_money),
        }

    @staticmethod
    def _gate_fields(gate: GateDecision) -> dict[str, Any]:
        return {
            "gate_approved": gate.approved,
            # Every check with its verdict, not only the failures. Knowing which
            # checks passed is what distinguishes "barely cleared" from "cleared
            # comfortably", and a rejection list alone cannot express either.
            "gate_results": {
                str(result.check): {
                    "passed": result.passed,
                    "detail": result.detail,
                    "mandatory": result.mandatory,
                }
                for result in gate.results
            },
            "gate_blocking": [str(result.check) for result in gate.blocking_failures],
        }

    @staticmethod
    def _exit_fields(decision: ExitDecision) -> dict[str, Any]:
        return {
            "position_id": str(decision.position_id),
            "action": str(decision.action),
            "exit_score": decision.exit_score,
            "fraction": decision.fraction,
            # The triggers that fired, not just the score they produced. A score of
            # 80 says how strongly; the triggers say why, and only one of those can
            # be argued with later.
            "triggers": list(decision.triggers),
        }
