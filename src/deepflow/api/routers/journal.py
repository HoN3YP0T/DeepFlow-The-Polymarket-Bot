"""Journal browsing. Section 23.

The decision log, rejections included. The rejected set is the evidence for whether the
gates are calibrated: the trades taken are a biased sample of those seen, and without the
refusals there is no way to tell a gate that is correctly protective from one that is
never satisfied.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from deepflow.adapters.persistence.repositories import SqlUnitOfWork
from deepflow.api.deps import ViewerPrincipal, get_audit, require_orchestrator
from deepflow.api.schemas import AuditEntryView
from deepflow.pipeline.orchestrator import Orchestrator

router = APIRouter(prefix="/journal", tags=["journal"])


@router.get("")
async def list_entries(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
    kind: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[dict[str, Any]]:
    """Decision log, newest first, filterable by ENTERED / REJECTED / HELD / EXITED.

    ``limit`` is capped at 500 by the query constraint rather than trusted: an unbounded
    limit turns a dashboard refresh into a full table scan of a time series.
    """
    sessions = orchestrator.sessions
    if sessions is None:
        return []
    async with sessions() as session:
        rows = await SqlUnitOfWork(session).journal.list_recent(limit=limit)
    entries = [dict(row) for row in rows]
    if kind:
        wanted = kind.upper()
        entries = [entry for entry in entries if str(entry.get("kind", "")).upper() == wanted]
    return entries


@router.get("/audit", response_model=list[AuditEntryView])
async def list_audit(
    principal: ViewerPrincipal,
    audit: Annotated[object, Depends(get_audit)],
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[AuditEntryView]:
    """Who did what through the dashboard.

    Readable from the thing it audits on purpose: an operator who cannot see what was done
    last has to ask someone, and during an incident that is the slowest path to the answer.
    """
    rows = await audit.recent(limit=limit)  # type: ignore[attr-defined]
    return [AuditEntryView(**row) for row in rows]
