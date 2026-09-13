"""Position manager.

Maintains open positions, marks them to market, and drives the exit engine's
reevaluation loop.

Marking uses the price at which the position could actually be *closed* -- the
bid we would hit, not the mid. On a wide outcome book the difference is the gap
between a position that looks profitable and one that is.
"""

from __future__ import annotations

from decimal import Decimal

from deepflow.core.domain import ExitDecision, MarketSnapshot, Position
from deepflow.core.logging import get_logger
from deepflow.core.types import PositionId
from deepflow.ports.repository import PositionRepository
from deepflow.positions.exit_engine import ExitEngine

log = get_logger(__name__)


class PositionManager:
    """Tracks and reevaluates open positions."""

    def __init__(self, *, repository: PositionRepository, exit_engine: ExitEngine) -> None:
        self._repository = repository
        self._exit_engine = exit_engine

    async def reevaluate_all(self) -> list[ExitDecision]:
        """Reevaluate every open position.

        Runs on every relevant market update, not on a timer. The events this
        system must react to -- a goal, a wicket, a break of serve -- are
        instantaneous, and a polling interval is a window in which a position
        is held against known-bad information.
        """
        raise NotImplementedError("PositionManager.reevaluate_all")

    async def mark_to_market(self, position: Position, snapshot: MarketSnapshot) -> Decimal:
        """Unrealized P&L at the realistic exit price."""
        raise NotImplementedError("PositionManager.mark_to_market")

    async def apply(self, decision: ExitDecision) -> None:
        """Act on an exit decision."""
        raise NotImplementedError("PositionManager.apply")

    async def close(self, position_id: PositionId, *, reason: str) -> None:
        raise NotImplementedError("PositionManager.close")
