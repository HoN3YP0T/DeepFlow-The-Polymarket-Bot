"""The dashboard's risk controls.

These are the buttons someone presses during an incident. The tests are about the four
ways they could betray that moment: acting for someone not allowed to, acting on a
mis-click, reporting success without acting, and resuming past the thing that noticed the
problem.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from deepflow.api.app import create_app
from deepflow.api.auth import Principal, hash_password
from deepflow.api.routers.risk import CONFIRMATIONS
from deepflow.config.settings import Settings
from deepflow.core.clock import SystemClock
from deepflow.core.enums import BreakerReason
from deepflow.pipeline.orchestrator import Orchestrator
from deepflow.risk.circuit_breakers import CircuitBreakerRegistry
from deepflow.risk.exposure import ExposureTracker
from deepflow.risk.limits import RiskEngine

PASSWORD = "s3cret-operator-pass"
SECRET = "k" * 48


class _RecordingAudit:
    """Stands in for the database-backed log, and records what it was asked to write."""

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
def rig(password_hash: str) -> tuple[TestClient, Orchestrator, _RecordingAudit]:
    settings = Settings(
        _env_file=None,
        api={  # type: ignore[arg-type]
            "jwt_secret": SECRET,
            "operators": {
                "boss": {"password_hash": password_hash, "role": "ADMIN"},
                "op": {"password_hash": password_hash, "role": "OPERATOR"},
                "eyes": {"password_hash": password_hash, "role": "VIEWER"},
            },
        },
    )
    orchestrator = Orchestrator(settings=settings)
    orchestrator._breakers = CircuitBreakerRegistry(settings.thresholds.breakers, SystemClock())
    orchestrator._risk = RiskEngine(settings.thresholds.risk, ExposureTracker())

    app = create_app(settings, orchestrator=orchestrator)
    audit = _RecordingAudit()
    app.state.audit = audit
    return TestClient(app), orchestrator, audit


def _headers(client: TestClient, username: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
    return {"Authorization": f"Bearer {response.json()['token']}"}


# --- who may do what -----------------------------------------------------
@pytest.mark.parametrize(
    ("route", "least_role"),
    [
        ("pause", "op"),
        ("resume", "op"),
        ("emergency-stop", "boss"),
        ("cancel-all-orders", "boss"),
        ("close-all-positions", "boss"),
    ],
)
def test_a_viewer_can_operate_nothing(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit], route: str, least_role: str
) -> None:
    client, _, _ = rig
    response = client.post(
        f"/api/risk/{route}",
        json={"confirm": CONFIRMATIONS[route]},
        headers=_headers(client, "eyes"),
    )
    assert response.status_code == 403
    # The refusal names what was required: hiding it turns a permissions problem into a
    # debugging session during an incident.
    assert "requires" in response.json()["detail"]


@pytest.mark.parametrize("route", ["emergency-stop", "cancel-all-orders", "close-all-positions"])
def test_an_operator_cannot_reach_the_admin_controls(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit], route: str
) -> None:
    client, _, _ = rig
    response = client.post(
        f"/api/risk/{route}",
        json={"confirm": CONFIRMATIONS[route]},
        headers=_headers(client, "op"),
    )
    assert response.status_code == 403


def test_every_control_requires_authentication(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    client, _, _ = rig
    for route in CONFIRMATIONS:
        if route == "limits":
            continue
        assert client.post(f"/api/risk/{route}", json={"confirm": "x"}).status_code == 401


# --- confirmation --------------------------------------------------------
@pytest.mark.parametrize("route", list(CONFIRMATIONS))
def test_each_action_has_its_own_confirmation_phrase(route: str) -> None:
    """A shared "yes" trains an operator to type it without reading, and then Close All
    Positions is one stale form submission away."""
    assert len(set(CONFIRMATIONS.values())) == len(CONFIRMATIONS)
    assert CONFIRMATIONS[route].isupper()


@pytest.mark.parametrize("supplied", ["", "yes", "y", "PAUSE PLEASE", "pause", "Pause"])
def test_the_wrong_confirmation_refuses(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit], supplied: str
) -> None:
    """Case-sensitive on purpose: accepting ``pause`` makes the confirmation a formality,
    which is the one thing it must not be."""
    client, orchestrator, _ = rig
    response = client.post(
        "/api/risk/pause", json={"confirm": supplied}, headers=_headers(client, "op")
    )
    assert response.status_code == 400
    assert orchestrator.breakers is not None
    assert orchestrator.breakers.entries_allowed(), "a refused control must not have acted"


def test_surrounding_whitespace_is_forgiven(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    """A trailing space from a copy-paste is not a mis-click."""
    client, _, _ = rig
    response = client.post(
        "/api/risk/pause", json={"confirm": "  PAUSE  "}, headers=_headers(client, "op")
    )
    assert response.status_code == 200


# --- what the controls actually do ---------------------------------------
def test_pause_trips_the_registry_the_trading_path_consults(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    """The same breaker an automatic halt uses, not a parallel flag. A second mechanism
    would be a switch wired to nothing."""
    client, orchestrator, _ = rig
    assert orchestrator.breakers is not None
    assert orchestrator.breakers.entries_allowed()

    response = client.post(
        "/api/risk/pause",
        json={"confirm": "PAUSE", "reason": "spread blew out"},
        headers=_headers(client, "op"),
    )
    assert response.json()["applied"] is True
    assert response.json()["entries_halted"] is True
    assert not orchestrator.breakers.entries_allowed()
    assert BreakerReason.MANUAL_HALT in orchestrator.breakers.open_reasons


def test_pausing_never_blocks_an_exit(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    """Pausing must not trap a position -- halting entries while blocking exits converts a
    risk control into the risk."""
    client, orchestrator, _ = rig
    client.post("/api/risk/pause", json={"confirm": "PAUSE"}, headers=_headers(client, "op"))
    assert orchestrator.breakers is not None
    assert orchestrator.breakers.exits_allowed()


def test_resume_clears_a_manual_halt(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    client, orchestrator, _ = rig
    headers = _headers(client, "op")
    client.post("/api/risk/pause", json={"confirm": "PAUSE"}, headers=headers)
    response = client.post("/api/risk/resume", json={"confirm": "RESUME"}, headers=headers)
    assert response.json()["applied"] is True
    assert orchestrator.breakers is not None
    assert orchestrator.breakers.entries_allowed()


def test_resume_refuses_while_a_real_fault_is_open(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    """**The control that must not be a way around the thing that noticed a problem.**

    A breaker that opened because the feed died is refusing for a reason resuming does not
    address. A dashboard button that cleared it would make the whole breaker catalogue
    advisory.
    """
    client, orchestrator, _ = rig
    assert orchestrator.breakers is not None
    orchestrator.breakers.trip(BreakerReason.WEBSOCKET_FAILURE, "feed died")
    orchestrator.breakers.trip(BreakerReason.MANUAL_HALT, "paused")

    response = client.post(
        "/api/risk/resume", json={"confirm": "RESUME"}, headers=_headers(client, "op")
    )
    assert response.json()["applied"] is False
    assert "websocket_failure" in response.json()["detail"]
    # And it left both breakers exactly as they were.
    assert not orchestrator.breakers.entries_allowed()
    assert BreakerReason.MANUAL_HALT in orchestrator.breakers.open_reasons


def test_resuming_when_nothing_is_paused_is_not_a_lie(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    client, _, _ = rig
    response = client.post(
        "/api/risk/resume", json={"confirm": "RESUME"}, headers=_headers(client, "op")
    )
    assert response.json()["applied"] is False
    assert "nothing to resume" in response.json()["detail"]


def test_emergency_stop_halts_entries_and_leaves_positions_alone(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    """Dumping a book into a thin market is itself a way to lose money, so closing is a
    separate, deliberate decision."""
    client, orchestrator, _ = rig
    response = client.post(
        "/api/risk/emergency-stop",
        json={"confirm": "EMERGENCY STOP", "reason": "something is wrong"},
        headers=_headers(client, "boss"),
    )
    assert response.json()["applied"] is True
    assert orchestrator.breakers is not None
    assert not orchestrator.breakers.entries_allowed()
    assert "Positions are untouched" in response.json()["detail"]


@pytest.mark.parametrize("route", ["cancel-all-orders", "close-all-positions"])
def test_a_control_that_cannot_act_says_so(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit], route: str
) -> None:
    """**The failure mode this guards against.**

    No execution adapter is constructed in this process, so there are no orders to cancel
    and no write path to cancel them with. An operator hitting Cancel All during an
    incident and seeing a cheerful 200 would reasonably conclude their exposure was
    withdrawn. ``applied: false`` with the reason is the honest answer.
    """
    client, _, _ = rig
    response = client.post(
        f"/api/risk/{route}",
        json={"confirm": CONFIRMATIONS[route]},
        headers=_headers(client, "boss"),
    )
    assert response.status_code == 200
    assert response.json()["applied"] is False
    assert "no execution adapter" in response.json()["detail"]


# --- audit ---------------------------------------------------------------
@pytest.mark.parametrize(
    ("route", "action", "username"),
    [
        ("pause", "risk.pause", "op"),
        ("resume", "risk.resume", "op"),
        ("emergency-stop", "risk.emergency_stop", "boss"),
        ("cancel-all-orders", "risk.cancel_all_orders", "boss"),
        ("close-all-positions", "risk.close_all_positions", "boss"),
    ],
)
def test_every_control_is_audited_with_its_actor(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
    route: str,
    action: str,
    username: str,
) -> None:
    client, _, audit = rig
    client.post(
        f"/api/risk/{route}",
        json={"confirm": CONFIRMATIONS[route], "reason": "because"},
        headers=_headers(client, username),
    )
    assert (username, action, {"reason": "because"}) in audit.entries


def test_a_refused_control_is_not_audited_as_an_action(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    """A confirmation failure never reached the action, so recording one would put an
    event in the trail that did not happen."""
    client, _, audit = rig
    client.post("/api/risk/pause", json={"confirm": "nope"}, headers=_headers(client, "op"))
    assert audit.entries == []


# --- limits --------------------------------------------------------------
def test_limits_can_be_read_and_changed(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    client, orchestrator, _ = rig
    before = client.get("/api/risk/limits", headers=_headers(client, "eyes")).json()
    assert before["max_open_positions"] == 10

    response = client.patch(
        "/api/risk/limits",
        json={"confirm": "UPDATE LIMITS", "max_open_positions": 3},
        headers=_headers(client, "op"),
    )
    assert response.json()["applied"] is True
    assert orchestrator.risk is not None
    assert orchestrator.risk.limits.max_open_positions == 3


def test_an_omitted_field_is_left_alone(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    """A whole-object replacement would reset limits the operator never touched, using
    whatever stale values their form was rendered with."""
    client, orchestrator, _ = rig
    client.patch(
        "/api/risk/limits",
        json={"confirm": "UPDATE LIMITS", "max_open_positions": 3},
        headers=_headers(client, "op"),
    )
    assert orchestrator.risk is not None
    assert (
        orchestrator.risk.limits.max_position_fraction
        == Settings(_env_file=None).thresholds.risk.max_position_fraction
    )


@pytest.mark.parametrize(
    "body",
    [
        {"max_position_fraction": "1.5"},
        {"max_position_fraction": "0"},
        {"max_daily_loss_fraction": "-0.1"},
        {"max_open_positions": 0},
        {"bankroll_usdc": "-1"},
    ],
)
def test_an_out_of_range_limit_is_refused(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit], body: dict[str, Any]
) -> None:
    client, _, _ = rig
    response = client.patch(
        "/api/risk/limits",
        json={"confirm": "UPDATE LIMITS", **body},
        headers=_headers(client, "op"),
    )
    assert response.status_code == 422


def test_raising_the_bankroll_never_erases_the_drawdown(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    """The peak only rises. Resetting it to a new balance would wipe the drawdown the
    limit is measured against, turning a breach into a clean slate at the worst moment."""
    client, orchestrator, _ = rig
    headers = _headers(client, "op")
    client.patch(
        "/api/risk/limits",
        json={"confirm": "UPDATE LIMITS", "bankroll_usdc": "1000"},
        headers=headers,
    )
    client.patch(
        "/api/risk/limits",
        json={"confirm": "UPDATE LIMITS", "bankroll_usdc": "600"},
        headers=headers,
    )
    assert orchestrator.risk is not None
    assert orchestrator.risk.bankroll.peak_balance_usdc == 1000
    assert orchestrator.risk.bankroll.drawdown_fraction == Decimal("0.4")


def test_changing_nothing_reports_that_it_changed_nothing(
    rig: tuple[TestClient, Orchestrator, _RecordingAudit],
) -> None:
    client, _, _ = rig
    response = client.patch(
        "/api/risk/limits",
        json={"confirm": "UPDATE LIMITS"},
        headers=_headers(client, "op"),
    )
    assert response.json()["applied"] is False


# --- without an orchestrator --------------------------------------------
def test_controls_refuse_when_the_api_runs_alone(password_hash: str) -> None:
    """``make api`` starts the dashboard for frontend work. A control with nothing to act
    on must refuse rather than report success against a registry nobody consults."""
    settings = Settings(
        _env_file=None,
        api={  # type: ignore[arg-type]
            "jwt_secret": SECRET,
            "operators": {"boss": {"password_hash": password_hash, "role": "ADMIN"}},
        },
    )
    client = TestClient(create_app(settings))
    headers = _headers(client, "boss")
    response = client.post("/api/risk/pause", json={"confirm": "PAUSE"}, headers=headers)
    assert response.status_code == 503
    assert "no orchestrator" in response.json()["detail"]
