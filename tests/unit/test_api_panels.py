"""The read-only panels, and the distinction they exist to preserve.

Most of these panels have no data behind them, and that is the point. An empty table and
a subsystem that does not run look identical on screen, and which one it is happens to be
the entire question an operator opens the panel to answer. The tests below are mostly
about keeping those two apart.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from deepflow.api.app import create_app
from deepflow.api.auth import Principal, hash_password
from deepflow.config.settings import Settings
from deepflow.core.clock import SystemClock
from deepflow.pipeline.orchestrator import Orchestrator
from deepflow.risk.circuit_breakers import CircuitBreakerRegistry
from deepflow.risk.exposure import ExposureTracker
from deepflow.risk.limits import RiskEngine

PASSWORD = "s3cret-operator-pass"
SECRET = "k" * 48


class _Audit:
    def __init__(self) -> None:
        self.entries: list[tuple[str, str, dict[str, Any]]] = []

    async def record(
        self, principal: Principal, action: str, *, detail: dict[str, Any] | None = None
    ) -> None:
        self.entries.append((principal.subject, action, dict(detail or {})))

    async def recent(self, *, limit: int = 100) -> list[dict[str, Any]]:
        return []


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def rig(password_hash: str) -> tuple[TestClient, Orchestrator, _Audit]:
    settings = Settings(
        _env_file=None,
        api={  # type: ignore[arg-type]
            "jwt_secret": SECRET,
            "operators": {
                "op": {"password_hash": password_hash, "role": "OPERATOR"},
                "eyes": {"password_hash": password_hash, "role": "VIEWER"},
            },
        },
    )
    orchestrator = Orchestrator(settings=settings)
    orchestrator._breakers = CircuitBreakerRegistry(settings.thresholds.breakers, SystemClock())
    orchestrator._risk = RiskEngine(settings.thresholds.risk, ExposureTracker())

    app = create_app(settings, orchestrator=orchestrator)
    audit = _Audit()
    app.state.audit = audit
    return TestClient(app), orchestrator, audit


def _headers(client: TestClient, username: str = "op") -> dict[str, str]:
    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    return {"Authorization": f"Bearer {r.json()['token']}"}


# --- every panel is authenticated ---------------------------------------
@pytest.mark.parametrize(
    "path",
    [
        "/api/overview",
        "/api/health/components",
        "/api/strategies",
        "/api/strategies/thresholds",
        "/api/trades/available",
        "/api/trades/rejected",
        "/api/trades/blockers",
        "/api/positions",
        "/api/whales/activity",
        "/api/journal",
        "/api/journal/audit",
    ],
)
def test_every_panel_requires_a_token(
    rig: tuple[TestClient, Orchestrator, _Audit], path: str
) -> None:
    """The overview carries balance, available capital and drawdown. "Read-only" is not
    "safe to publish" -- a reader who can reach the port can see the size of the book."""
    client, _, _ = rig
    assert client.get(path).status_code == 401


# --- absence is reported, not rendered as emptiness ----------------------
def test_positions_says_it_does_not_run_rather_than_showing_nothing(
    rig: tuple[TestClient, Orchestrator, _Audit],
) -> None:
    """**The distinction the envelope exists for.**

    No position manager runs and ``SqlPositionRepository`` is unimplemented, so nothing
    can write the table. A bare ``[]`` cannot say that, and rendering "no positions" for
    a missing subsystem reports an absence as good news.
    """
    client, _, _ = rig
    body = client.get("/api/positions", headers=_headers(client)).json()
    assert body["available"] is False
    assert "no position can be opened" in body["detail"]
    assert body["positions"] == []


def test_whales_says_nothing_polls_it(rig: tuple[TestClient, Orchestrator, _Audit]) -> None:
    """The engine is implemented and verified (§75); what is missing is a poll. "No whale
    activity" and "nothing is looking for whale activity" are different claims."""
    client, _, _ = rig
    body = client.get("/api/whales/activity", headers=_headers(client)).json()
    assert body["available"] is False
    assert "nothing polls it" in body["detail"]


def test_an_unknown_wallet_never_returns_a_zeroed_profile(
    rig: tuple[TestClient, Orchestrator, _Audit],
) -> None:
    """A zeroed score card reads as "we scored this wallet and it is unremarkable", which
    is the opposite of "we have never seen it".

    Two honest refusals, and which one depends on whether we could look at all. This
    orchestrator was never started, so it holds no session factory: the answer is 503
    ("cannot look"), not 404 ("looked, found nothing"). Collapsing the two would be the
    same mistake this whole module is about.
    """
    client, _, _ = rig
    response = client.get("/api/whales/wallets/0xabc", headers=_headers(client))
    assert response.status_code == 503
    assert "no database" in response.json()["detail"]


# --- strategies ----------------------------------------------------------
def test_only_registered_engines_are_listed(
    rig: tuple[TestClient, Orchestrator, _Audit],
) -> None:
    """The stub this replaced named ten strategies, most of which are not constructed. A
    dashboard that lists a strategy nobody runs invites you to believe it is running."""
    client, orchestrator, _ = rig
    rows = client.get("/api/strategies", headers=_headers(client)).json()
    assert rows == []  # nothing registered on this bare orchestrator

    from deepflow.config.thresholds import Btc5mThresholds
    from deepflow.engines.crypto.btc_5m import Btc5mEngine
    from deepflow.engines.crypto.reference import TwapReference
    from deepflow.engines.registry import EngineRegistry

    registry = EngineRegistry()
    registry.register(Btc5mEngine(Btc5mThresholds(), reference=TwapReference()))
    orchestrator._engines = registry

    rows = client.get("/api/strategies", headers=_headers(client)).json()
    assert [r["name"] for r in rows] == ["btc_5m"]
    assert rows[0]["enabled"] is True
    # Honest about running on raw output until an operator activates a fit.
    assert rows[0]["calibrated"] is False


def _with_engine(orchestrator: Orchestrator) -> None:
    from deepflow.config.thresholds import Btc5mThresholds
    from deepflow.engines.crypto.btc_5m import Btc5mEngine
    from deepflow.engines.crypto.reference import TwapReference
    from deepflow.engines.registry import EngineRegistry

    registry = EngineRegistry()
    registry.register(Btc5mEngine(Btc5mThresholds(), reference=TwapReference()))
    orchestrator._engines = registry


def test_a_viewer_cannot_toggle_a_strategy(
    rig: tuple[TestClient, Orchestrator, _Audit],
) -> None:
    client, orchestrator, _ = rig
    _with_engine(orchestrator)
    response = client.post(
        "/api/strategies/btc_5m/toggle",
        json={"enabled": False, "confirm": "TOGGLE STRATEGY"},
        headers=_headers(client, "eyes"),
    )
    assert response.status_code == 403


def test_a_toggle_needs_its_confirmation(rig: tuple[TestClient, Orchestrator, _Audit]) -> None:
    client, orchestrator, _ = rig
    _with_engine(orchestrator)
    response = client.post(
        "/api/strategies/btc_5m/toggle",
        json={"enabled": False, "confirm": "yes"},
        headers=_headers(client),
    )
    assert response.status_code == 400
    assert orchestrator.engines is not None
    assert orchestrator.engines.is_enabled("btc_5m"), "a refused toggle must not have acted"


def test_toggling_an_unknown_engine_is_404(rig: tuple[TestClient, Orchestrator, _Audit]) -> None:
    """A toggle that reports success for a misspelt engine tells an operator they disabled
    something they did not."""
    client, orchestrator, _ = rig
    _with_engine(orchestrator)
    response = client.post(
        "/api/strategies/btc_5me/toggle",
        json={"enabled": False, "confirm": "TOGGLE STRATEGY"},
        headers=_headers(client),
    )
    assert response.status_code == 404


def test_a_disabled_engine_stops_being_resolved(
    rig: tuple[TestClient, Orchestrator, _Audit],
) -> None:
    """**Disabling must stop the engine forming an opinion, not just acting on one.**

    Otherwise its markets stay in the decision context, get priced, and fill the journal
    with decisions about a strategy nobody is running.
    """
    from decimal import Decimal

    from deepflow.core.domain import Classification
    from deepflow.core.enums import MarketCategory

    client, orchestrator, _ = rig
    _with_engine(orchestrator)
    registry = orchestrator.engines
    assert registry is not None
    classification = Classification(
        category=MarketCategory.BTC_5M, confidence=Decimal("0.95"), rationale="tag"
    )
    assert registry.resolve(classification) is not None

    client.post(
        "/api/strategies/btc_5m/toggle",
        json={"enabled": False, "confirm": "TOGGLE STRATEGY"},
        headers=_headers(client),
    )
    assert registry.resolve(classification) is None
    assert not registry.is_enabled("btc_5m")


def test_a_toggle_is_audited(rig: tuple[TestClient, Orchestrator, _Audit]) -> None:
    client, orchestrator, audit = rig
    _with_engine(orchestrator)
    client.post(
        "/api/strategies/btc_5m/toggle",
        json={"enabled": False, "confirm": "TOGGLE STRATEGY", "reason": "noisy"},
        headers=_headers(client),
    )
    assert audit.entries[-1][1] == "strategy.toggle"
    assert audit.entries[-1][2]["engine"] == "btc_5m"
    assert audit.entries[-1][2]["enabled"] is False


def test_thresholds_are_read_only(rig: tuple[TestClient, Orchestrator, _Audit]) -> None:
    """No general "patch any threshold" route. Most of these are measured values with a
    finding behind them, and a form that quietly overwrites one turns a measurement into
    a guess."""
    client, _, _ = rig
    app_paths = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    assert "patch" not in app_paths.get("/api/strategies/thresholds", {})
    body = client.get("/api/strategies/thresholds", headers=_headers(client)).json()
    assert "risk" in body and "btc_5m" in body


# --- websocket -----------------------------------------------------------
def test_the_socket_refuses_before_accepting(
    rig: tuple[TestClient, Orchestrator, _Audit],
) -> None:
    """Closed before accept. A socket that accepts first and checks later has already
    told an unauthenticated client the endpoint exists and is willing to talk."""
    from starlette.websockets import WebSocketDisconnect

    client, _, _ = rig
    for query in ("", "?token=garbage"):
        with pytest.raises(WebSocketDisconnect), client.websocket_connect("/ws" + query) as ws:
            ws.receive_json()


def test_the_socket_pushes_status_to_an_authenticated_client(
    rig: tuple[TestClient, Orchestrator, _Audit],
) -> None:
    client, _, _ = rig
    token = client.post("/api/auth/login", json={"username": "op", "password": PASSWORD}).json()[
        "token"
    ]
    with client.websocket_connect(f"/ws?token={token}") as ws:
        frame = ws.receive_json()
    assert frame["available"] is True
    assert frame["entries_halted"] is False
    assert "counters" in frame
