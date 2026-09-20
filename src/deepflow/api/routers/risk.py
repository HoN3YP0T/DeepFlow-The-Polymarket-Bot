"""Risk controls. Section 22.

Pause, resume, cancel all orders, close all positions, adjust limits, emergency stop.

Every action requires a role, an explicit typed confirmation, and an audit row written
**before** anything happens. These are the controls someone reaches for during an
incident, when hesitation is expensive and a mis-click is equally so.

**Three rules the shape of this module comes from.**

*A control that cannot act says so.* ``cancel-all-orders`` and ``close-all-positions``
have nothing to act on today -- no execution adapter is constructed in the running process
-- so they return ``applied: false`` with the reason. They do not raise, and they do not
report success. An operator hitting Cancel All during an incident and seeing a cheerful
200 would reasonably conclude their orders were cancelled.

*Resume cannot resume past a fault.* It clears only the manual halt. If any other breaker
is open -- a dead feed, a database failure, a daily loss limit -- resume refuses and names
them, because a dashboard button must not be a way around the thing that noticed a problem.

*Nothing here arms LIVE mode.* Hard rule 1. There is no such route, and
``tests/unit/test_api_auth.py`` asserts its absence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Final

from fastapi import APIRouter, Depends, HTTPException, Request, status

from deepflow.api.audit import AuditLog
from deepflow.api.auth import Principal
from deepflow.api.deps import (
    AdminPrincipal,
    OperatorPrincipal,
    ViewerPrincipal,
    get_audit,
    get_orchestrator,
    require_orchestrator,
)
from deepflow.api.schemas import (
    ControlResponse,
    RiskActionRequest,
    RiskLimitsUpdate,
    RiskLimitsView,
)
from deepflow.core.enums import BreakerReason
from deepflow.core.logging import get_logger
from deepflow.pipeline.orchestrator import Orchestrator

log = get_logger(__name__)

router = APIRouter(prefix="/risk", tags=["risk"])

#: The phrase each destructive control requires in ``confirm``.
#:
#: Per-action and not a shared "yes": a dashboard that confirms everything with the same
#: word trains an operator to type it without reading, and then Close All Positions is one
#: stale form submission away. Typing ``CLOSE ALL POSITIONS`` is a sentence you cannot
#: produce by accident.
CONFIRMATIONS: Final[dict[str, str]] = {
    "pause": "PAUSE",
    "resume": "RESUME",
    "cancel-all-orders": "CANCEL ALL ORDERS",
    "close-all-positions": "CLOSE ALL POSITIONS",
    "emergency-stop": "EMERGENCY STOP",
    "limits": "UPDATE LIMITS",
}


def _require_confirmation(action: str, supplied: str) -> None:
    """Reject anything but the exact phrase for this action.

    Compared case-sensitively and after stripping surrounding whitespace only. Accepting
    ``"emergency stop"`` for ``EMERGENCY STOP`` would make the confirmation a formality,
    which is the one thing it must not be.
    """
    expected = CONFIRMATIONS[action]
    if supplied.strip() != expected:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"this action requires confirm={expected!r}",
        )


def _breaker_names(orchestrator: Orchestrator) -> tuple[str, ...]:
    breakers = orchestrator.breakers
    return tuple(str(reason) for reason in breakers.open_reasons) if breakers else ()


def _response(
    action: str,
    *,
    applied: bool,
    detail: str,
    principal: Principal,
    orchestrator: Orchestrator | None = None,
) -> ControlResponse:
    breakers = orchestrator.breakers if orchestrator is not None else None
    return ControlResponse(
        action=action,
        applied=applied,
        detail=detail,
        actor=principal.subject,
        at=datetime.now(UTC),
        entries_halted=(not breakers.entries_allowed()) if breakers else None,
        open_breakers=_breaker_names(orchestrator) if orchestrator is not None else (),
    )


@router.get("/state", response_model=ControlResponse)
async def read_state(principal: ViewerPrincipal, request: Request) -> ControlResponse:
    """Whether entries are halted, and which breakers are open. Read-only."""
    orchestrator = get_orchestrator(request)
    if orchestrator is None:
        return _response(
            "state", applied=False, detail="no orchestrator in this process", principal=principal
        )
    return _response(
        "state", applied=True, detail="ok", principal=principal, orchestrator=orchestrator
    )


@router.post("/pause", response_model=ControlResponse)
async def pause_new_trades(
    payload: RiskActionRequest,
    principal: OperatorPrincipal,
    audit: Annotated[AuditLog, Depends(get_audit)],
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> ControlResponse:
    """Halt new entries. Exits continue -- pausing must never trap a position.

    Trips the same ``MANUAL_HALT`` breaker the trading path consults, so this is the
    identical mechanism an automatic halt uses rather than a parallel flag.
    """
    _require_confirmation("pause", payload.confirm)
    await audit.record(principal, "risk.pause", detail={"reason": payload.reason})

    breakers = orchestrator.breakers
    if breakers is None:
        return _response(
            "pause",
            applied=False,
            detail="no breaker registry; the orchestrator has not finished starting",
            principal=principal,
        )
    breakers.trip(BreakerReason.MANUAL_HALT, payload.reason or f"paused by {principal.subject}")
    return _response(
        "pause",
        applied=True,
        detail="new entries halted; exits continue",
        principal=principal,
        orchestrator=orchestrator,
    )


@router.post("/resume", response_model=ControlResponse)
async def resume_trading(
    payload: RiskActionRequest,
    principal: OperatorPrincipal,
    audit: Annotated[AuditLog, Depends(get_audit)],
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> ControlResponse:
    """Resume entries. Refused while any other breaker is still open.

    Clears **only** the manual halt. A breaker that opened because the feed died or the
    database failed is refusing for a reason that resuming does not address, and a button
    that cleared it would make the whole breaker catalogue advisory.
    """
    _require_confirmation("resume", payload.confirm)
    await audit.record(principal, "risk.resume", detail={"reason": payload.reason})

    breakers = orchestrator.breakers
    if breakers is None:
        return _response("resume", applied=False, detail="no breaker registry", principal=principal)

    blocking = tuple(r for r in breakers.open_reasons if r is not BreakerReason.MANUAL_HALT)
    if blocking:
        return _response(
            "resume",
            applied=False,
            detail=(
                "refused: "
                + ", ".join(str(r) for r in blocking)
                + " still open. These are not cleared from the dashboard -- fix the cause."
            ),
            principal=principal,
            orchestrator=orchestrator,
        )

    cleared = breakers.reset(BreakerReason.MANUAL_HALT, actor=principal.subject)
    return _response(
        "resume",
        applied=cleared,
        detail="entries resumed" if cleared else "nothing to resume; no manual halt was set",
        principal=principal,
        orchestrator=orchestrator,
    )


@router.post("/emergency-stop", response_model=ControlResponse)
async def emergency_stop(
    payload: RiskActionRequest,
    principal: AdminPrincipal,
    audit: Annotated[AuditLog, Depends(get_audit)],
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> ControlResponse:
    """Halt entries and latch the manual breaker.

    **Deliberately does not force-close positions.** Dumping a book into a thin market is
    itself a way to lose money, and the operator decides that separately via
    ``close-all-positions``. Cancelling resting orders is part of the intent and is
    reported honestly: there is nothing to cancel while execution is unwired.
    """
    _require_confirmation("emergency-stop", payload.confirm)
    await audit.record(principal, "risk.emergency_stop", detail={"reason": payload.reason})

    breakers = orchestrator.breakers
    if breakers is None:
        return _response(
            "emergency-stop", applied=False, detail="no breaker registry", principal=principal
        )
    breakers.trip(
        BreakerReason.MANUAL_HALT,
        payload.reason or f"emergency stop by {principal.subject}",
    )
    log.error("api.emergency_stop", actor=principal.subject, reason=payload.reason)
    return _response(
        "emergency-stop",
        applied=True,
        detail=(
            "entries halted and latched. No resting orders were cancelled: no execution "
            "adapter exists in this process, so none can exist to cancel. Positions are "
            "untouched by design."
        ),
        principal=principal,
        orchestrator=orchestrator,
    )


@router.post("/cancel-all-orders", response_model=ControlResponse)
async def cancel_all_orders(
    payload: RiskActionRequest,
    principal: AdminPrincipal,
    audit: Annotated[AuditLog, Depends(get_audit)],
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> ControlResponse:
    """Cancel every resting order.

    Returns ``applied: false`` today, and that is the honest answer rather than a
    placeholder: this process constructs no execution adapter, so it holds no orders and
    has no authenticated write path to cancel any. Reporting success would tell an operator
    their exposure was withdrawn when it was not -- and if orders ever did exist, that lie
    is the most expensive one this API could tell.
    """
    _require_confirmation("cancel-all-orders", payload.confirm)
    await audit.record(principal, "risk.cancel_all_orders", detail={"reason": payload.reason})
    return _response(
        "cancel-all-orders",
        applied=False,
        detail=(
            "no execution adapter in this process: there are no orders to cancel, and no "
            "write path to cancel them with. This will act once Phase 5 writes are verified."
        ),
        principal=principal,
        orchestrator=orchestrator,
    )


@router.post("/close-all-positions", response_model=ControlResponse)
async def close_all_positions(
    payload: RiskActionRequest,
    principal: AdminPrincipal,
    audit: Annotated[AuditLog, Depends(get_audit)],
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> ControlResponse:
    """Exit every open position. ADMIN only, typed confirmation required.

    Also ``applied: false`` today, for the same reason, and with one more: no position
    manager runs in this process either, so there is no book to close.
    """
    _require_confirmation("close-all-positions", payload.confirm)
    await audit.record(principal, "risk.close_all_positions", detail={"reason": payload.reason})
    return _response(
        "close-all-positions",
        applied=False,
        detail=(
            "no execution adapter and no position manager in this process: there is no "
            "book to close. Emergency-stop has halted entries; exits are unaffected."
        ),
        principal=principal,
        orchestrator=orchestrator,
    )


@router.get("/limits", response_model=RiskLimitsView)
async def read_limits(
    principal: ViewerPrincipal,
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> RiskLimitsView:
    """The limits the next decision will be judged against."""
    risk = orchestrator.risk
    if risk is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the risk engine has not been constructed yet",
        )
    limits = risk.limits
    return RiskLimitsView(
        bankroll_usdc=risk.bankroll.balance_usdc,
        max_position_fraction=limits.max_position_fraction,
        max_total_exposure_fraction=limits.max_total_exposure_fraction,
        max_daily_loss_fraction=limits.max_daily_loss_fraction,
        max_open_positions=limits.max_open_positions,
    )


@router.patch("/limits", response_model=ControlResponse)
async def update_limits(
    payload: RiskLimitsUpdate,
    principal: OperatorPrincipal,
    audit: Annotated[AuditLog, Depends(get_audit)],
    orchestrator: Annotated[Orchestrator, Depends(require_orchestrator)],
) -> ControlResponse:
    """Change the live risk limits.

    A partial update: omitted fields are left alone. A whole-object PUT would happily reset
    a limit the operator never meant to touch, using whatever stale value their form was
    rendered with.

    **Takes effect from the next decision and unwinds nothing already open.** Worth saying
    in the response, because an operator who tightens a cap mid-incident may believe they
    have just reduced their exposure. They have reduced what the system will add to it.
    """
    _require_confirmation("limits", payload.confirm)
    risk = orchestrator.risk
    if risk is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="the risk engine has not been constructed yet",
        )

    changes = {
        field: value
        for field, value in (
            ("max_position_fraction", payload.max_position_fraction),
            ("max_total_exposure_fraction", payload.max_total_exposure_fraction),
            ("max_daily_loss_fraction", payload.max_daily_loss_fraction),
            ("max_open_positions", payload.max_open_positions),
        )
        if value is not None
    }
    await audit.record(
        principal,
        "risk.update_limits",
        detail={"reason": payload.reason, "changes": {k: str(v) for k, v in changes.items()}}
        | (
            {"bankroll_usdc": str(payload.bankroll_usdc)}
            if payload.bankroll_usdc is not None
            else {}
        ),
    )

    if changes:
        risk.replace_limits(risk.limits.model_copy(update=changes))
    if payload.bankroll_usdc is not None:
        state = risk.bankroll
        risk.update_bankroll(
            type(state)(
                balance_usdc=payload.bankroll_usdc,
                # The peak only ever rises: resetting it to the new balance would erase the
                # drawdown the limit is measured against, turning a limit breach into a
                # clean slate at exactly the wrong moment.
                peak_balance_usdc=max(state.peak_balance_usdc, payload.bankroll_usdc),
                realized_pnl_today=state.realized_pnl_today,
            )
        )

    if not changes and payload.bankroll_usdc is None:
        return _response(
            "limits",
            applied=False,
            detail="nothing to change: every field was omitted",
            principal=principal,
            orchestrator=orchestrator,
        )
    return _response(
        "limits",
        applied=True,
        detail=(
            "applied from the next decision. Nothing already open is unwound -- a tighter "
            "limit reduces what the system will add, not what it holds."
        ),
        principal=principal,
        orchestrator=orchestrator,
    )
