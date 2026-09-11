"""Overview panel. Section 22."""

from __future__ import annotations

from fastapi import APIRouter

from deepflow.api.schemas import OverviewResponse

router = APIRouter(prefix="/overview", tags=["overview"])


@router.get("", response_model=OverviewResponse)
async def get_overview() -> OverviewResponse:
    """Balance, capital, exposure, P&L, drawdown and system status."""
    raise NotImplementedError("overview.get_overview")
