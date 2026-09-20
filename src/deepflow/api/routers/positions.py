"""Open Positions panel. Section 22.

Reads the ``positions`` table, which is empty and will stay empty until two things exist:
``SqlPositionRepository`` (three stubs) and a position manager constructed in the running
process. Both are tracked in ``docs/STATUS.md``.

**The panel says that rather than rendering an empty list.** An empty table and a subsystem
that does not run look identical, and the difference is the whole question an operator is
asking when they open this panel. ``available`` in the response is what distinguishes them.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from deepflow.adapters.persistence.models import JournalRow, PositionRow
from deepflow.api.deps import ViewerPrincipal, require_orchestrator
from deepflow.pipeline.orchestrator import Orchestrator

router = APIRouter(prefix="/positions", tags=["positions"])

_UNAVAILABLE = (
    "No position manager runs in this process and SqlPositionRepository is unimplemented, "
    "so no position can be opened or stored. An empty list here is the absence of a "
    "subsystem, not a flat book."
)


@router.get("")
async def list_positions(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> dict[str, Any]:
    """Open positions with entry, mark, P&L and exit score.

    Returns an envelope rather than a bare list so the panel can distinguish "none open"
    from "this does not run yet". A bare ``[]`` cannot carry that, and rendering the two
    the same way is how a dashboard quietly reports a missing subsystem as good news.
    """
    sessions = orchestrator.sessions
    if sessions is None:
        return {"available": False, "detail": _UNAVAILABLE, "positions": []}

    async with sessions() as session:
        rows = (await session.execute(select(PositionRow))).scalars().all()

    return {
        # False while nothing can write the table. Flipping this is part of wiring the
        # position manager, not a display tweak.
        "available": False,
        "detail": _UNAVAILABLE,
        "positions": [
            {
                "position_id": row.position_id,
                "condition_id": row.condition_id,
                "shares": str(row.shares),
                "average_entry_price": str(row.average_entry_price),
                "entry_probability": str(row.entry_probability),
                "realized_pnl": str(row.realized_pnl),
                "run_mode": row.run_mode,
                "opened_at": row.opened_at,
            }
            for row in rows
        ],
    }


@router.get("/{position_id}/journal")
async def position_journal(
    position_id: str,
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """Full decision history for one position: why entered, held, exited.

    Keyed on the journal's own ``position_id`` column, so this works the moment positions
    exist -- the journal has carried the column since Phase 4.
    """
    sessions = orchestrator.sessions
    if sessions is None:
        return []
    async with sessions() as session:
        rows = (
            (
                await session.execute(
                    select(JournalRow)
                    .where(JournalRow.position_id == position_id)
                    .order_by(JournalRow.id.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "reason": row.reason,
            "context": row.context,
            "recorded_at": row.recorded_at,
        }
        for row in rows
    ]
