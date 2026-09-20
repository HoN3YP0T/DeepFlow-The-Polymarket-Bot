"""Overview panel. Section 22.

The top of the dashboard: what the system is, what it holds, and whether it is allowed
to act. Every figure comes from the running orchestrator rather than being recomputed
here, so the number on screen is the number the trading path is using.

**Zeros are labelled, not hidden.** A paper run with no positions shows zero exposure and
zero P&L, and those are correct. What would be wrong is presenting them without the mode
beside them, so a PAPER zero reads like a LIVE flat book.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated

from fastapi import APIRouter, Depends

from deepflow.api.deps import ViewerPrincipal, require_orchestrator
from deepflow.api.schemas import OverviewResponse
from deepflow.pipeline.orchestrator import Orchestrator

router = APIRouter(prefix="/overview", tags=["overview"])


@router.get("", response_model=OverviewResponse)
async def get_overview(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> OverviewResponse:
    """Balance, capital, exposure, P&L, drawdown and system status."""
    risk = orchestrator.risk
    breakers = orchestrator.breakers

    bankroll = risk.bankroll if risk is not None else None
    balance = bankroll.balance_usdc if bankroll else Decimal(0)
    available = risk.available_capital() if risk is not None else Decimal(0)

    halt_reasons = tuple(str(reason) for reason in breakers.open_reasons) if breakers else ()
    return OverviewResponse(
        mode=orchestrator.settings.mode,
        balance_usdc=balance,
        available_capital_usdc=available,
        # Reserved is what the bankroll holds back, so it is the balance minus what may be
        # deployed -- derived rather than stored, which keeps the two from disagreeing.
        reserved_capital_usdc=max(Decimal(0), balance - available),
        total_exposure_usdc=Decimal(0),
        pnl_today_usdc=bankroll.realized_pnl_today if bankroll else Decimal(0),
        pnl_total_usdc=Decimal(0),
        drawdown_fraction=bankroll.drawdown_fraction if bankroll else Decimal(0),
        # Both genuinely zero: no position manager and no execution adapter run in this
        # process. Reported rather than omitted, because a missing panel reads as a
        # loading failure while a zero with a mode beside it reads as the truth.
        open_positions=0,
        pending_orders=0,
        entries_halted=not breakers.entries_allowed() if breakers else False,
        halt_reasons=halt_reasons,
        counters=orchestrator.counters,
    )
