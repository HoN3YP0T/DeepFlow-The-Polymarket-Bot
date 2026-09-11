"""Available Trades panel. Sections 12, 22.

Live opportunity cards. Smart-money activity is shown explicitly on each card,
with the exact odds each wallet paid -- a whale filling at 0.887 and one
filling at 0.94 are different pieces of evidence, and rounding them to "bought
high" throws away the distinction.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from deepflow.api.schemas import AvailableTradeCard

router = APIRouter(prefix="/trades", tags=["trades"])


@router.get("/available", response_model=list[AvailableTradeCard])
async def list_available_trades() -> list[AvailableTradeCard]:
    """Current candidate trades with full supporting evidence."""
    raise NotImplementedError("trades.list_available_trades")


@router.get("/rejected")
async def list_rejected() -> list[dict[str, Any]]:
    """Recently rejected opportunities and the checks that blocked them.

    Surfaced in the UI rather than buried in logs: the rejected set is how an
    operator sees whether the gates are protecting capital or just never
    firing.
    """
    raise NotImplementedError("trades.list_rejected")
