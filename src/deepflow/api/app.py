"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from deepflow.api import ws
from deepflow.api.audit import AuditLog
from deepflow.api.routers import (
    auth_routes,
    health,
    journal,
    overview,
    positions,
    risk,
    strategies,
    trades,
    whales,
)
from deepflow.config.settings import Settings, get_settings
from deepflow.core.logging import get_logger

if TYPE_CHECKING:
    from deepflow.pipeline.orchestrator import Orchestrator

log = get_logger(__name__)

#: Where the single-page dashboard lives.
STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire runtime components into application state."""
    log.info("api.starting", mode=str(app.state.settings.mode))
    yield
    log.info("api.stopping")


def create_app(
    settings: Settings | None = None,
    *,
    orchestrator: Orchestrator | None = None,
) -> FastAPI:
    """Build the dashboard API.

    ``orchestrator`` is the running system this dashboard controls. It is optional because
    ``make api`` starts the API alone for frontend work -- and when it is absent every
    control refuses rather than pretending, which is what
    :func:`deepflow.api.deps.require_orchestrator` enforces.

    The controls only mean anything when both run in **one process**: the API holds a
    reference to the orchestrator's breaker registry and risk engine. ``main.py`` wires
    them together, and its docstring claimed to for some time before it did.
    """
    settings = settings or get_settings()

    app = FastAPI(
        title="DeepFlow",
        version="0.1.0",
        summary="Polymarket quantitative trading system",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.orchestrator = orchestrator
    # Sessions come from the orchestrator when there is one, so the API never opens a
    # second engine against the same database.
    app.state.audit = AuditLog(
        orchestrator.sessions if orchestrator is not None else None,
    )

    # Origins are an explicit allowlist. The API exposes trading kill switches,
    # so a wildcard here would be a real vulnerability rather than a convenience.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.api.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH"],
        allow_headers=["Authorization", "Content-Type"],
    )

    for router in (
        auth_routes.router,
        overview.router,
        trades.router,
        positions.router,
        whales.router,
        strategies.router,
        risk.router,
        health.router,
        journal.router,
    ):
        app.include_router(router, prefix="/api")
    app.include_router(ws.router)

    @app.get("/", include_in_schema=False)
    async def dashboard() -> FileResponse:
        """The dashboard itself.

        One self-contained file, served by the API that feeds it. No build step and no
        second origin, which also means the CORS allowlist is not load-bearing for the
        dashboard's own use -- it is same-origin.
        """
        return FileResponse(STATIC_DIR / "index.html")

    return app
