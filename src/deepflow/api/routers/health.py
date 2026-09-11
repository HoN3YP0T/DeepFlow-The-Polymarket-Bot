"""System health. Section 22."""

from __future__ import annotations

from fastapi import APIRouter

from deepflow.api.schemas import HealthResponse

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Process liveness. Intentionally trivial -- a liveness probe that checks
    dependencies restarts the process when a dependency blips."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness() -> dict[str, str]:
    """Readiness: DB, Redis, SDK session and reconciliation state."""
    raise NotImplementedError("health.readiness")


@router.get("/components", response_model=HealthResponse)
async def components() -> HealthResponse:
    """Per-component status with latency, data age, reconnects and errors."""
    raise NotImplementedError("health.components")
