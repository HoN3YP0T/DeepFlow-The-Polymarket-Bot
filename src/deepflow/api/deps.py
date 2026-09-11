"""FastAPI dependency providers.

Runtime components are resolved from application state rather than
module-level globals, so tests can substitute fakes and the API never
constructs a second copy of anything the orchestrator owns.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, Request

from deepflow.api.auth import Principal, Role
from deepflow.config.settings import Settings


def get_app_settings(request: Request) -> Settings:
    return request.app.state.settings  # type: ignore[no-any-return]


def get_orchestrator(request: Request) -> object:
    return request.app.state.orchestrator


async def get_principal(request: Request) -> Principal:
    """Resolve and verify the caller."""
    raise NotImplementedError("deps.get_principal")


def require_role(role: Role) -> Callable[[Principal], Awaitable[Principal]]:
    """Dependency factory enforcing a minimum role.

    Used on the risk-control routes, where the difference between VIEWER and
    ADMIN is the difference between reading the dashboard and closing the book.
    """

    async def _dependency(
        principal: Annotated[Principal, Depends(get_principal)],
    ) -> Principal:
        raise NotImplementedError("deps.require_role")

    return _dependency
