"""Dashboard WebSocket.

Pushes live updates so the Available Trades, Open Positions and Whale panels
reflect current state without polling.

Fed from the internal :class:`~deepflow.core.bus.EventBus`, which drops for a
slow subscriber rather than applying backpressure. A dashboard tab left open on
a laptop that went to sleep must not be able to stall market-data ingest.
"""

from __future__ import annotations

from fastapi import APIRouter, WebSocket

from deepflow.core.logging import get_logger

log = get_logger(__name__)

router = APIRouter(tags=["stream"])


@router.websocket("/ws")
async def dashboard_stream(websocket: WebSocket) -> None:
    """Stream trade cards, position updates, whale activity and health.

    TODO(skeleton): authenticate before accepting (the socket carries the same
    data as the authenticated REST endpoints), subscribe to the bus, and fan
    out with a per-client send timeout.
    """
    raise NotImplementedError("ws.dashboard_stream")
