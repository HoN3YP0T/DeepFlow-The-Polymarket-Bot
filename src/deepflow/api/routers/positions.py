"""Open Positions panel. Section 22."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from deepflow.api.schemas import OpenPositionCard

router = APIRouter(prefix="/positions", tags=["positions"])


@router.get("", response_model=list[OpenPositionCard])
async def list_positions() -> list[OpenPositionCard]:
    """Open positions with entry, mark, P&L, exit score and event state."""
    raise NotImplementedError("positions.list_positions")


@router.get("/{position_id}/journal")
async def position_journal(position_id: str) -> list[dict[str, Any]]:
    """Full decision history for one position: why entered, held, exited."""
    raise NotImplementedError("positions.position_journal")
