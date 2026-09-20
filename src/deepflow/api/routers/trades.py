"""Available Trades panel. Sections 12, 22.

Opportunity cards and, just as prominently, the ones that were refused.

**Built from the journal, not from live state.** An "available trade" here is a decision
the system actually reached and recorded, which is a stronger claim than a candidate it
might reach on the next tick: the journal row carries the probability, the edge, the cost
stack and every gate result as they stood at that instant. A panel rebuilt from current
state would show a card whose numbers no longer match any decision anyone made.

Smart-money activity is shown with the exact odds each wallet paid -- a whale filling at
0.887 and one filling at 0.94 are different pieces of evidence, and rounding them to
"bought high" throws away the distinction.
"""

from __future__ import annotations

import collections
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from deepflow.adapters.persistence.repositories import SqlUnitOfWork
from deepflow.api.deps import ViewerPrincipal, require_orchestrator
from deepflow.pipeline.orchestrator import Orchestrator

router = APIRouter(prefix="/trades", tags=["trades"])

#: How many journal rows to read when filling a panel.
#:
#: The journal is overwhelmingly refusals -- 33,686 of 40,021 rows in one run -- so a
#: window sized for the panel would return refusals and no approvals. Read wide, filter,
#: then truncate.
SCAN = 2_000


async def _rows(orchestrator: Orchestrator, limit: int) -> list[dict[str, Any]]:
    sessions = orchestrator.sessions
    if sessions is None:
        return []
    async with sessions() as session:
        return [dict(row) for row in await SqlUnitOfWork(session).journal.list_recent(limit=limit)]


def _titles(orchestrator: Orchestrator) -> dict[str, str]:
    """Condition id -> question, from the markets the process is tracking.

    The journal stores a condition id and no title, and a card showing a 66-character hex
    string is a card nobody can read. A market that has since dropped out of the tracked
    set simply has no title; it is shown by id rather than omitted, because a decision
    that happened is not less real for the market having closed.
    """
    return {str(market.condition_id): market.question for market in orchestrator.tracked_markets}


def _card(row: dict[str, Any], titles: dict[str, str]) -> dict[str, Any]:
    context = row.get("context") or {}
    condition_id = str(row.get("condition_id") or "")
    return {
        "recorded_at": row.get("recorded_at"),
        "condition_id": condition_id,
        "title": titles.get(condition_id) or f"({condition_id[:18]}… no longer tracked)",
        "category": context.get("category"),
        "engine": context.get("engine"),
        "action": context.get("action"),
        "market_probability": context.get("market_probability"),
        "model_probability": context.get("calibrated_probability"),
        "edge": context.get("edge"),
        "net_ev": context.get("net_ev"),
        "confidence": context.get("ev_confidence"),
        "target_price": context.get("target_price"),
        "cost_total_bps": context.get("cost_total_bps"),
        "uncertainty": context.get("uncertainty"),
        "smart_money": context.get("smart_money"),
        "blocking": context.get("gate_blocking") or [],
        "reason": row.get("reason"),
    }


@router.get("/available")
async def list_available_trades(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, Any]]:
    """Trades the full chain approved, newest first, with the evidence behind each.

    ``ENTERED`` rather than a live candidate list: these are decisions that passed all
    eighteen checks and the risk engine. In PAPER nothing was submitted, which is what the
    mode badge on the dashboard is for.
    """
    titles = _titles(orchestrator)
    rows = await _rows(orchestrator, SCAN)
    approved = [r for r in rows if str(r.get("kind")) == "ENTERED"]
    return [_card(r, titles) for r in approved[:limit]]


@router.get("/rejected")
async def list_rejected(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> list[dict[str, Any]]:
    """Recently refused opportunities and the checks that blocked them.

    Surfaced rather than buried in logs: the rejected set is how an operator sees whether
    the gates are protecting capital or simply never firing. A system that refuses
    everything and one that has no opportunities look identical from the approvals alone.
    """
    titles = _titles(orchestrator)
    rows = await _rows(orchestrator, SCAN)
    refused = [r for r in rows if str(r.get("kind")) == "REJECTED"]
    return [_card(r, titles) for r in refused[:limit]]


@router.get("/blockers")
async def rejection_summary(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> dict[str, Any]:
    """Which checks refuse most often, over the recent journal.

    The question an operator actually has: *what is stopping us?* One check accounting
    for nearly every refusal is a threshold to examine; refusals spread evenly across
    many is a market that genuinely offers nothing. Both look like "no trades today" on
    every other panel.

    A decision can be blocked by several checks at once, so the counts sum to more than
    the number of refusals -- each is "how often did this check refuse", not a share.
    """
    rows = await _rows(orchestrator, SCAN)
    refused = [r for r in rows if str(r.get("kind")) == "REJECTED"]
    counts: collections.Counter[str] = collections.Counter()
    for row in refused:
        for check in (row.get("context") or {}).get("gate_blocking") or []:
            counts[str(check)] += 1
    return {
        "scanned": len(rows),
        "rejected": len(refused),
        "approved": sum(1 for r in rows if str(r.get("kind")) == "ENTERED"),
        "blockers": [{"check": name, "count": n} for name, n in counts.most_common()],
    }
