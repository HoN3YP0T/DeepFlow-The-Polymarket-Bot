"""FastAPI dependency providers.

Runtime components are resolved from application state rather than module-level globals,
so tests can substitute fakes and the API never constructs a second copy of anything the
orchestrator owns.

**The decision this module encodes.** ``Settings`` requires a JWT secret in SHADOW and
LIVE and allows PAPER without one, so a local paper run needs no credentials. That cannot
mean the controls are open: an unauthenticated emergency-stop is a remote kill switch for
anyone who can reach the port. So without a configured secret the API serves the read-only
panels and **refuses every control** -- fail-closed on the half that acts, usable on the
half that only looks.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import Depends, HTTPException, Request, status

from deepflow.api.audit import AuditLog
from deepflow.api.auth import Principal, Role, verify_token
from deepflow.config.settings import Settings
from deepflow.core.errors import AuthenticationError
from deepflow.core.logging import get_logger

if TYPE_CHECKING:
    from deepflow.pipeline.orchestrator import Orchestrator

log = get_logger(__name__)


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_orchestrator(request: Request) -> Orchestrator | None:
    """The running orchestrator, or ``None`` when the API runs on its own.

    ``None`` is a real state rather than a failure: ``make api`` starts the dashboard
    alone for frontend work. Every reader must handle it, and every control refuses on it
    -- a control with nothing to act on must say so, not report success.
    """
    orchestrator: Orchestrator | None = request.app.state.orchestrator
    return orchestrator


def get_audit(request: Request) -> AuditLog:
    """The audit log, from application state.

    From state rather than constructed per request so it shares the orchestrator's session
    factory -- a second engine against the same database would double the pool.
    """
    audit: AuditLog = request.app.state.audit
    return audit


def require_orchestrator(request: Request) -> Orchestrator:
    """The orchestrator, or a 503.

    For endpoints that are meaningless without it. 503 rather than 500: nothing is broken,
    the thing being asked about is not running here.
    """
    orchestrator = get_orchestrator(request)
    if orchestrator is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no orchestrator in this process; start with `deepflow` rather than `make api`",
        )
    return orchestrator


def _bearer_token(request: Request) -> str | None:
    header = request.headers.get("Authorization") or ""
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


async def get_principal(request: Request) -> Principal:
    """Resolve and verify the caller, or raise 401.

    A 401 carries ``WWW-Authenticate: Bearer`` so a client knows what to present, and
    never says *why* the credential failed -- absent, malformed, expired and forged are one
    answer to the caller and four different lines in the log.
    """
    settings = get_app_settings(request)
    secret = settings.api.jwt_secret

    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="authentication required",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if secret is None:
        log.warning("api.auth_unconfigured", path=request.url.path)
        raise unauthorized

    token = _bearer_token(request)
    if token is None:
        raise unauthorized

    try:
        return verify_token(token, secret=secret.get_secret_value())
    except AuthenticationError as error:
        log.warning("api.auth_failed", path=request.url.path, reason=str(error))
        raise unauthorized from error


def require_role(role: Role) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory enforcing a minimum role.

    Used on the risk-control routes, where the difference between VIEWER and ADMIN is the
    difference between reading the dashboard and closing the book.

    The refusal is 403 and it names what was required. Hiding that turns an operator's
    permissions problem into a debugging session during an incident, which is precisely
    when nobody has time for one.
    """

    async def _dependency(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> Principal:
        if not principal.can(role):
            log.warning(
                "api.authz_refused",
                subject=principal.subject,
                held=principal.role.value,
                required=role.value,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"requires {role.value}; caller holds {principal.role.value}",
            )
        return principal

    return _dependency


#: Pre-built dependencies, so a route reads as its own permission.
ViewerPrincipal = Annotated[Principal, Depends(require_role(Role.VIEWER))]
OperatorPrincipal = Annotated[Principal, Depends(require_role(Role.OPERATOR))]
AdminPrincipal = Annotated[Principal, Depends(require_role(Role.ADMIN))]
