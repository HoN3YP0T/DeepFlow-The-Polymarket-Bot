"""System health. Section 22.

Per-component status, assembled from what the orchestrator actually holds rather than by
probing anything. A dashboard that re-probes the venue to render a panel adds load to the
thing it is reporting on, and reports the health of its own probe rather than the feed the
trading path is using.

**The statuses are the ones that change a decision.** UP, DEGRADED, DOWN, UNKNOWN -- and
UNKNOWN is a real answer, used wherever a component is not constructed in this process.
Reporting an absent component as DOWN would look like a fault; reporting it as UP would be
a lie.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Request

from deepflow.api.deps import ViewerPrincipal, get_orchestrator, require_orchestrator
from deepflow.api.schemas import ComponentStatus, HealthResponse
from deepflow.core.enums import ComponentHealth
from deepflow.pipeline.orchestrator import Orchestrator

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def liveness() -> dict[str, str]:
    """Process liveness. Intentionally trivial -- a liveness probe that checks
    dependencies restarts the process when a dependency blips."""
    return {"status": "ok"}


@router.get("/ready")
async def readiness(request: Request) -> dict[str, str]:
    """Readiness: is this process able to do its job?

    Distinct from liveness, which only says the process is running. Not ready is not a
    fault -- a process still warming up its volatility series is healthy and cannot yet
    price anything.
    """
    orchestrator = get_orchestrator(request)
    if orchestrator is None:
        return {"status": "not_ready", "detail": "no orchestrator in this process"}
    streams = orchestrator.streams
    if streams is None or not streams.is_connected:
        return {"status": "not_ready", "detail": "market stream not connected"}
    return {"status": "ready"}


@router.get("/components", response_model=HealthResponse)
async def components(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> HealthResponse:
    """Per-component status with data age, reconnects and drops."""
    now = datetime.now(UTC)
    streams = orchestrator.streams
    breakers = orchestrator.breakers
    reference = orchestrator.reference
    counters = orchestrator.counters

    rows: list[ComponentStatus] = []

    if streams is None:
        rows.append(
            ComponentStatus(
                name="market stream", status=ComponentHealth.UNKNOWN, detail="not constructed"
            )
        )
    else:
        last = streams.last_event_at
        age = (now - last).total_seconds() if last else None
        rows.append(
            ComponentStatus(
                name="market stream",
                # Dropped events are not a warning to be noted and moved past: every
                # overflow marks every book gapped, and the entry gate then refuses all of
                # it (§91). A feed that is connected and dropping is DEGRADED, not UP.
                status=(
                    ComponentHealth.UP
                    if streams.is_connected and streams.dropped_events == 0
                    else ComponentHealth.DEGRADED
                    if streams.is_connected
                    else ComponentHealth.DOWN
                ),
                data_age_seconds=age,
                reconnect_count=streams.reconnect_count,
                error_count=streams.dropped_events,
                detail=f"{counters['snapshots_written']} snapshots written",
            )
        )

    rows.append(
        ComponentStatus(
            name="reference feed (TWAP)",
            status=ComponentHealth.UP
            if reference and reference.symbols()
            else ComponentHealth.DOWN,
            detail=(f"{reference.symbols()} symbols held" if reference else "not constructed"),
        )
    )

    rows.append(
        ComponentStatus(
            name="database",
            status=ComponentHealth.UP
            if orchestrator.sessions is not None
            else ComponentHealth.DOWN,
            # Surfaced beside the status because a journal that silently swallows write
            # failures once reported 3,642 decisions and wrote none (§83).
            error_count=counters["journal_failures"] + counters["prediction_failures"],
            detail=f"{counters['journal_failures']} journal write failures",
        )
    )

    rows.append(
        ComponentStatus(
            name="circuit breakers",
            status=(
                ComponentHealth.UP
                if breakers and breakers.entries_allowed()
                else ComponentHealth.DEGRADED
                if breakers
                else ComponentHealth.UNKNOWN
            ),
            detail=(
                ", ".join(str(r) for r in breakers.open_reasons) or "none open"
                if breakers
                else "not constructed"
            ),
        )
    )

    rows.append(
        ComponentStatus(
            name="probability engines",
            status=(ComponentHealth.UP if counters["estimates"] > 0 else ComponentHealth.DEGRADED),
            detail=(
                f"{counters['priceable_markets']} priceable markets, "
                f"{counters['estimates']} estimates, {counters['decisions']} decisions"
            ),
        )
    )

    # Named rather than omitted. These are the two halves that do not run in this process,
    # and a dashboard that simply leaves them out invites the reader to assume they work.
    for name in ("execution", "position manager"):
        rows.append(
            ComponentStatus(
                name=name,
                status=ComponentHealth.UNKNOWN,
                detail="not constructed in this process; no orders can be placed",
            )
        )

    return HealthResponse(components=tuple(rows), checked_at=now)
