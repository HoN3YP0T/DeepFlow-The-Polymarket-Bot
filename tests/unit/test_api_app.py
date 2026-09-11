"""Application wiring tests.

Guards against a router being added but never included -- a silent failure that
shows up as a 404 in the dashboard rather than as an error at startup.
"""

from __future__ import annotations

from deepflow.api.app import create_app
from deepflow.config.settings import Settings

EXPECTED_PATHS = {
    "/api/overview",
    "/api/trades/available",
    "/api/trades/rejected",
    "/api/positions",
    "/api/whales/activity",
    "/api/strategies",
    "/api/strategies/thresholds",
    "/api/risk/pause",
    "/api/risk/resume",
    "/api/risk/cancel-all-orders",
    "/api/risk/close-all-positions",
    "/api/risk/emergency-stop",
    "/api/health/live",
    "/api/health/ready",
    "/api/health/components",
    "/api/journal",
}


def _app():
    return create_app(Settings(_env_file=None))


def test_every_dashboard_panel_has_an_endpoint() -> None:
    paths = set(_app().openapi()["paths"])
    assert paths >= EXPECTED_PATHS


def test_cors_origins_are_an_explicit_allowlist() -> None:
    """The API exposes trading kill switches, so a wildcard origin would be a
    real vulnerability rather than a convenience."""
    settings = Settings(_env_file=None)
    assert "*" not in settings.api.cors_origins


def test_liveness_does_not_depend_on_anything() -> None:
    """A liveness probe that checks dependencies restarts the process whenever
    a dependency blips."""
    import asyncio

    from deepflow.api.routers.health import liveness

    assert asyncio.run(liveness()) == {"status": "ok"}
