"""Strategy controls. Section 22.

Enable or disable each engine and read the thresholds it is judged against.

Threshold changes are audited. A quiet loosening of a risk limit is indistinguishable
from a strategy change in the P&L afterwards, so the record of who changed what, and
when, is what makes results interpretable.

**Disabling an engine is not a halt, and the wording here says so.** It stops that engine
forming an opinion; it does nothing about a position already open, and it does not stop
the other engines. The halt is ``/risk/pause``, which is a different control with a
different confirmation.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status

from deepflow.api.audit import AuditLog
from deepflow.api.deps import (
    OperatorPrincipal,
    ViewerPrincipal,
    get_audit,
    require_orchestrator,
)
from deepflow.api.schemas import EngineView, StrategyToggleRequest
from deepflow.pipeline.orchestrator import Orchestrator

router = APIRouter(prefix="/strategies", tags=["strategies"])

#: What a toggle must carry to take effect.
TOGGLE_CONFIRMATION = "TOGGLE STRATEGY"


def _views(orchestrator: Orchestrator) -> list[EngineView]:
    registry = orchestrator.engines
    if registry is None:
        return []
    return [
        EngineView(
            name=engine.name,
            categories=tuple(sorted(str(c) for c in engine.categories)),
            enabled=registry.is_enabled(engine.name),
            # An engine runs on its raw output until an operator activates a fit. Shown
            # per engine because "calibrated" is not a property of the system -- one
            # engine can have a curve while the others do not.
            calibrated=type(getattr(engine, "_calibrator", None)).__name__ == "IsotonicCalibrator",
        )
        for engine in registry.engines
    ]


@router.get("", response_model=list[EngineView])
async def list_strategies(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> list[EngineView]:
    """Every registered engine, what it claims, and whether it is running.

    Only engines the registry actually holds. The stub this replaces listed ten by name
    -- football, cricket, tennis, badminton, BTC 5m, politics, geopolitics, whale,
    cross-market, auto execution -- most of which are not constructed, and a dashboard
    that lists a strategy nobody is running invites you to believe it is running.
    """
    return _views(orchestrator)


@router.post("/{name}/toggle", response_model=EngineView)
async def toggle_strategy(
    name: str,
    payload: StrategyToggleRequest,
    principal: OperatorPrincipal,
    audit: Annotated[AuditLog, Depends(get_audit)],
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> EngineView:
    """Turn one engine on or off, from the next sweep.

    A disabled engine abstains exactly as an absent one does, so its markets leave the
    decision context rather than being priced and refused -- otherwise the journal fills
    with decisions about a strategy nobody is running.
    """
    if payload.confirm.strip() != TOGGLE_CONFIRMATION:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"this action requires confirm={TOGGLE_CONFIRMATION!r}",
        )
    registry = orchestrator.engines
    if registry is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the engine registry has not been constructed yet",
        )

    await audit.record(
        principal,
        "strategy.toggle",
        detail={"engine": name, "enabled": payload.enabled, "reason": payload.reason},
    )
    if not registry.set_enabled(name, payload.enabled):
        # 404 rather than a silent no-op: a toggle that reports success for a misspelt
        # engine tells an operator they disabled something they did not.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"no engine named {name!r}",
        )

    # Rebuilt now rather than at the next sweep, so the effect is visible immediately in
    # the dashboard's own priceable-markets count.
    orchestrator.rebuild_decision_context()
    view = next((v for v in _views(orchestrator) if v.name == name), None)
    assert view is not None
    return view


@router.get("/thresholds")
async def get_thresholds(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> dict[str, Any]:
    """The tunables, as the running process holds them.

    Read-only here. The two that can be changed at runtime have their own routes with
    their own confirmations -- ``/risk/limits`` for the risk limits, and the toggle above
    for engines. A general "patch any threshold" endpoint is deliberately absent: most of
    these are measured values with a finding behind them (§87 for the event-market limits,
    §86 for the crypto ones), and a dashboard field that quietly overwrites one is how a
    measurement becomes a guess.
    """
    thresholds = orchestrator.settings.thresholds

    def _plain(model: Any) -> dict[str, Any]:
        return {
            key: str(value) if isinstance(value, Decimal) else value
            for key, value in model.model_dump().items()
            if not isinstance(value, dict | list | tuple)
        }

    return {
        "risk": _plain(thresholds.risk),
        "btc_5m": _plain(thresholds.btc_5m),
        "event_markets": _plain(thresholds.event_markets),
        "sports_football": _plain(thresholds.sports.football),
        "execution": _plain(thresholds.execution),
        "exits": _plain(thresholds.exits),
    }
