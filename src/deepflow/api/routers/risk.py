"""Risk controls. Section 22.

Pause, resume, cancel all orders, close all positions, disable a strategy,
emergency stop.

Every action here requires ADMIN or OPERATOR, an explicit typed confirmation,
and an audit entry. These are the controls someone reaches for during an
incident, when hesitation is expensive and a mis-click is equally so.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from deepflow.api.schemas import RiskActionRequest

router = APIRouter(prefix="/risk", tags=["risk"])


@router.post("/pause")
async def pause_new_trades(request: RiskActionRequest) -> dict[str, Any]:
    """Halt new entries. Exits continue -- pausing must never trap a position."""
    raise NotImplementedError("risk.pause_new_trades")


@router.post("/resume")
async def resume_trading(request: RiskActionRequest) -> dict[str, Any]:
    """Resume entries. Refused while any circuit breaker is still open."""
    raise NotImplementedError("risk.resume_trading")


@router.post("/cancel-all-orders")
async def cancel_all_orders(request: RiskActionRequest) -> dict[str, Any]:
    raise NotImplementedError("risk.cancel_all_orders")


@router.post("/close-all-positions")
async def close_all_positions(request: RiskActionRequest) -> dict[str, Any]:
    """Exit every open position. ADMIN only, typed confirmation required."""
    raise NotImplementedError("risk.close_all_positions")


@router.post("/emergency-stop")
async def emergency_stop(request: RiskActionRequest) -> dict[str, Any]:
    """Halt entries, cancel resting orders, and latch every breaker.

    Deliberately does not force-close positions: dumping a book into a thin
    market is itself a way to lose money, and the operator decides that
    separately via close-all-positions.
    """
    raise NotImplementedError("risk.emergency_stop")
