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
    """Writes the decision log."""

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
        raise NotImplementedError("JournalRecorder.record_entry")

    async def record_rejection(
        self, signal: Signal, *, gate: GateDecision, context: dict[str, Any] | None = None
    ) -> None:
        """Record a rejected opportunity and the failing checks."""
        raise NotImplementedError("JournalRecorder.record_rejection")

    async def record_hold(self, decision: ExitDecision) -> None:
        raise NotImplementedError("JournalRecorder.record_hold")

    async def record_exit(self, decision: ExitDecision, *, outcome: dict[str, Any]) -> None:
        raise NotImplementedError("JournalRecorder.record_exit")
