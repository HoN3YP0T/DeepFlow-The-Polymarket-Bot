"""Strategy controls. Section 22.

Enable/disable each strategy and adjust its thresholds at runtime.

Threshold changes are audited. A quiet loosening of a risk limit is
indistinguishable from a strategy change in the P&L afterwards, so the record
of who changed what, and when, is what makes results interpretable.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from deepflow.api.schemas import StrategyToggle

router = APIRouter(prefix="/strategies", tags=["strategies"])


@router.get("", response_model=list[StrategyToggle])
async def list_strategies() -> list[StrategyToggle]:
    """Football, cricket, tennis, badminton, BTC 5m, politics, geopolitics,
    whale engine, cross-market engine, auto execution."""
    raise NotImplementedError("strategies.list_strategies")


@router.post("/{name}/toggle", response_model=StrategyToggle)
async def toggle_strategy(name: str, enabled: bool) -> StrategyToggle:
    raise NotImplementedError("strategies.toggle_strategy")


@router.get("/thresholds")
async def get_thresholds() -> dict[str, Any]:
    raise NotImplementedError("strategies.get_thresholds")


@router.patch("/thresholds")
async def update_thresholds(patch: dict[str, Any]) -> dict[str, Any]:
    """Update thresholds at runtime. Validated and audited."""
    raise NotImplementedError("strategies.update_thresholds")
