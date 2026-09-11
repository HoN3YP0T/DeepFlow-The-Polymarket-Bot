"""Whale dashboard. Sections 11-12, 22.

Shows new whales, smart-money entries and, equally prominently, smart-money
exits. A scored wallet unwinding a position is at least as informative as one
opening it, and a dashboard that only shows entries makes following look
better than it is.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from deepflow.api.schemas import WhaleActivityCard

router = APIRouter(prefix="/whales", tags=["whales"])


@router.get("/activity", response_model=list[WhaleActivityCard])
async def list_activity() -> list[WhaleActivityCard]:
    """Recent smart-money entries and exits with exact odds."""
    raise NotImplementedError("whales.list_activity")


@router.get("/wallets/{wallet}")
async def wallet_detail(wallet: str) -> dict[str, Any]:
    """Scored wallet profile: performance, specialization, score components."""
    raise NotImplementedError("whales.wallet_detail")
