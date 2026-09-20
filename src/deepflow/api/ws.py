"""Dashboard WebSocket.

Pushes live status so the panels reflect current state without polling.

**Authenticated before the socket is accepted.** It carries the same data as the
authenticated REST endpoints -- capital, exposure, breaker state -- and a socket that
accepts first and checks later has already told an unauthenticated client that the
endpoint exists and is willing to talk. The token arrives as a query parameter because
browsers cannot set headers on a WebSocket handshake; it is never logged.

**A slow client is dropped, never waited for.** The send has a timeout and a client that
misses it is closed. A dashboard tab on a laptop that went to sleep must not be able to
apply backpressure to anything, and this socket shares an event loop with the market-data
fold -- which, as §91 records, is a loop that cannot afford to wait for anybody.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from fastapi import APIRouter, WebSocket, WebSocketDisconnect, status

from deepflow.api.auth import verify_token
from deepflow.core.errors import AuthenticationError
from deepflow.core.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["stream"])

#: How often a connected dashboard is pushed a status frame.
#:
#: Two seconds. The underlying figures are counters and breaker state, which change
#: continuously; pushing faster would spend the loop's time serialising numbers nobody
#: can read that quickly.
PUSH_SECONDS = 2.0

#: How long a single send may take before the client is considered gone.
#:
#: Generous for a healthy browser on a local network, and short enough that a suspended
#: laptop is dropped rather than held. The alternative -- an unbounded await -- is how a
#: dashboard tab stalls the event loop the trading path runs on.
SEND_TIMEOUT = 5.0


@router.websocket("/ws")
async def dashboard_stream(websocket: WebSocket, token: str = "") -> None:
    """Stream status frames: counters, capital and breaker state."""
    settings = websocket.app.state.settings
    secret = settings.api.jwt_secret

    if secret is None:
        # Consistent with the REST side: no secret means no session, and no session means
        # nothing here is reachable.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    try:
        principal = verify_token(token, secret=secret.get_secret_value())
    except AuthenticationError as error:
        log.warning("api.ws_auth_failed", reason=str(error))
        # Closed before accept: an unauthenticated client never gets a connected socket.
        await websocket.close(code=status.WS_1008_POLICY_VIOLATION)
        return

    await websocket.accept()
    log.info("api.ws_connected", subject=principal.subject)
    try:
        while True:
            orchestrator = websocket.app.state.orchestrator
            await asyncio.wait_for(websocket.send_json(_frame(orchestrator)), SEND_TIMEOUT)
            await asyncio.sleep(PUSH_SECONDS)
    except (TimeoutError, WebSocketDisconnect, RuntimeError):
        # A disconnect is ordinary; a timeout means the client stopped reading. Both end
        # the same way, and neither is worth an error line on a dashboard socket.
        log.info("api.ws_closed", subject=principal.subject)
    finally:
        with contextlib.suppress(Exception):
            await websocket.close()


def _frame(orchestrator: Any) -> dict[str, Any]:
    """One status frame.

    Deliberately small and derived from state the orchestrator already holds -- no
    database read per push. A socket that queries on every tick turns an idle dashboard
    into steady load on the system it is watching.
    """
    if orchestrator is None:
        return {"available": False}

    breakers = orchestrator.breakers
    risk = orchestrator.risk
    streams = orchestrator.streams
    return {
        "available": True,
        "entries_halted": (not breakers.entries_allowed()) if breakers else None,
        "open_breakers": [str(r) for r in breakers.open_reasons] if breakers else [],
        "balance_usdc": str(risk.bankroll.balance_usdc) if risk else None,
        "connected": streams.is_connected if streams else False,
        "dropped": streams.dropped_events if streams else 0,
        "counters": orchestrator.counters,
    }
