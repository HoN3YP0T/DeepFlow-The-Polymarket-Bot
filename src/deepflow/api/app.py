"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from deepflow.api import ws
from deepflow.api.routers import (
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

log = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Wire runtime components into application state."""
    log.info("api.starting", mode=str(app.state.settings.mode))
    yield
    log.info("api.stopping")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the dashboard API."""
    settings = settings or get_settings()

    app = FastAPI(
        title="DeepFlow",
        version="0.1.0",
        summary="Polymarket quantitative trading system",
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.orchestrator = None

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

    return app
