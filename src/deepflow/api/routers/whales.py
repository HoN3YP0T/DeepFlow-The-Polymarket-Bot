"""Whale dashboard. Sections 11-12, 22.

Shows new whales, smart-money entries and, equally prominently, smart-money **exits**. A
scored wallet unwinding a position is at least as informative as one opening it, and a
dashboard that only shows entries makes following look better than it is.

``SmartMoneyEngine`` is implemented and verified (§75, where a wallet down 964 USDC showed
100 of 100 winners because closed positions come back sorted by PnL descending). What does
not exist is a poll: nothing in the running process calls it, so ``smart_wallets`` and
``smart_money_events`` are empty. That is stated in the response rather than rendered as
"no whale activity", which is a different and much more reassuring claim.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select

from deepflow.adapters.persistence.models import SmartMoneyEventRow, SmartWalletRow
from deepflow.api.deps import ViewerPrincipal, require_orchestrator
from deepflow.pipeline.orchestrator import Orchestrator

router = APIRouter(prefix="/whales", tags=["whales"])

_UNAVAILABLE = (
    "The smart-money engine is implemented but nothing polls it in this process, so no "
    "wallet activity is being collected. An empty list is the absence of a poll, not an "
    "absence of whales."
)


@router.get("/activity")
async def list_activity(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    """Recent smart-money entries and exits with exact odds."""
    sessions = orchestrator.sessions
    if sessions is None:
        return {"available": False, "detail": _UNAVAILABLE, "events": []}

    async with sessions() as session:
        rows = (
            (
                await session.execute(
                    select(SmartMoneyEventRow)
                    .order_by(SmartMoneyEventRow.observed_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    return {
        "available": False,
        "detail": _UNAVAILABLE,
        "events": [
            {
                "wallet": row.wallet,
                "condition_id": row.condition_id,
                "side": row.side,
                "notional_usdc": str(row.notional_usdc),
                # The exact fill, never rounded: 0.887 and 0.94 are different evidence.
                "entry_price": str(row.entry_price),
                "market_probability_at_entry": str(row.market_probability_at_entry),
                "is_exit": row.is_exit,
                "observed_at": row.observed_at,
            }
            for row in rows
        ],
    }


@router.get("/wallets/{wallet}")
async def wallet_detail(
    wallet: str,
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> dict[str, Any]:
    """Scored wallet profile: performance, specialization, score components."""
    sessions = orchestrator.sessions
    if sessions is None:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="no database")

    async with sessions() as session:
        row = (
            await session.execute(select(SmartWalletRow).where(SmartWalletRow.wallet == wallet))
        ).scalar_one_or_none()

    if row is None:
        # 404 rather than an empty profile. A zeroed score card for an unknown wallet
        # reads as "we scored this wallet and it is unremarkable", which is the opposite
        # of "we have never seen it".
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no scored wallet {wallet!r}; nothing polls the smart-money engine yet",
        )
    return {
        "wallet": row.wallet,
        # Earned from being right, not from being large -- see SmartMoneyEngine, where
        # detection and scoring are deliberately kept apart.
        "smart_score": row.smart_score,
        "realized_pnl": str(row.realized_pnl) if row.realized_pnl is not None else None,
        "roi": str(row.roi) if row.roi is not None else None,
        "accuracy": str(row.accuracy) if row.accuracy is not None else None,
        "trade_count": row.trade_count,
        "category_specialization": row.category_specialization,
        "first_seen": row.first_seen,
        "last_scored_at": row.last_scored_at,
    }
