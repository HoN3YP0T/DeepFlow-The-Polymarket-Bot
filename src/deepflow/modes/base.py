"""Run-mode executors. Section 25.

The promotion path is BACKTEST -> PAPER -> SHADOW -> LIVE, and the modes differ
only at the final hop. Everything above -- discovery, classification,
resolution validation, probability, EV, safety, sizing, journalling -- runs
identically in every mode.

That identity is the point. If PAPER exercised a different code path from LIVE,
a clean paper run would prove nothing about the system that eventually trades.
SHADOW exists for the same reason at one level deeper: it builds and signs the
real order and suppresses only the network call, so the execution path itself
is tested before capital is at stake.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from deepflow.core.domain import OrderIntent, OrderRecord
from deepflow.core.enums import RunMode


class ModeExecutor(ABC):
    """Mode-specific final hop."""

    mode: RunMode

    @abstractmethod
    async def submit(self, intent: OrderIntent) -> OrderRecord:
        """Handle a fully-validated order intent."""

    @property
    def is_live(self) -> bool:
        return self.mode is RunMode.LIVE
