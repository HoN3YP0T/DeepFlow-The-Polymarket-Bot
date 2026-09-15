"""Login. Section 29.

One route, and the interesting parts are the refusals.

**A failed login takes the same work as a successful one.** The scrypt verification runs
against a decoy hash when the username is unknown, so "no such user" and "wrong password"
cannot be told apart by timing. Without that, an attacker enumerates valid operator names
before trying a single password.

**Every attempt is logged, success or failure**, with the username and the caller's
address. A brute-force attempt against an emergency-stop button should be visible in the
log the first time, not after it works.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from deepflow.api.auth import Principal, Role, create_token, hash_password, verify_password
from deepflow.api.deps import ViewerPrincipal, get_app_settings
from deepflow.api.schemas import LoginRequest, LoginResponse, WhoAmIResponse
from deepflow.config.settings import Settings
from deepflow.core.logging import get_logger

log = get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

#: A real hash of a value nobody knows, verified against when the username is unknown.
#:
#: Built once at import: the cost is one scrypt derivation at startup, and it buys a
#: constant-time answer to "does this user exist".
_DECOY_HASH = hash_password("decoy-never-matches-anything-at-all")


@router.post("/login", response_model=LoginResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    settings: Annotated[Settings, Depends(get_app_settings)],
) -> LoginResponse:
    """Exchange a username and password for a bearer token."""
    client = request.client.host if request.client else "unknown"
    secret = settings.api.jwt_secret

    if secret is None:
        # Consistent with deps.get_principal: with no secret there is no authenticated
        # session to issue, and the controls stay refused rather than open.
        log.warning("api.login_unconfigured", username=payload.username, client=client)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="no DEEPFLOW_API__JWT_SECRET configured; the dashboard is read-only",
        )

    operator = settings.api.operators.get(payload.username)
    stored = operator.password_hash if operator is not None else _DECOY_HASH
    # Always verify, even for an unknown user, so the two cases cost the same.
    matched = verify_password(payload.password, stored)

    if operator is None or not matched:
        log.warning(
            "api.login_failed",
            username=payload.username,
            client=client,
            reason="unknown user" if operator is None else "bad password",
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        role = Role(operator.role)
    except ValueError as error:
        # A role typo must not grant anything. Refused rather than defaulted to VIEWER,
        # because silently downgrading hides a configuration error that someone believes
        # granted more.
        log.error("api.login_bad_role", username=payload.username, role=operator.role)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"operator {payload.username!r} has an unrecognised role",
        ) from error

    principal = Principal(subject=payload.username, role=role)
    token = create_token(
        principal,
        secret=secret.get_secret_value(),
        ttl_seconds=settings.api.jwt_ttl_seconds,
    )
    log.info("api.login_ok", username=payload.username, role=role.value, client=client)
    return LoginResponse(
        token=token,
        role=role.value,
        expires_in_seconds=settings.api.jwt_ttl_seconds,
    )


@router.get("/whoami", response_model=WhoAmIResponse)
async def whoami(principal: ViewerPrincipal) -> WhoAmIResponse:
    """Who the current token says you are, and what it lets you do.

    The dashboard uses this to decide which controls to render. That is a convenience and
    never the enforcement: every control checks the role again server-side, because a
    hidden button is not a disabled one.
    """
    return WhoAmIResponse(
        subject=principal.subject,
        role=principal.role.value,
        may_operate=principal.can(Role.OPERATOR),
        may_administer=principal.can(Role.ADMIN),
    )
