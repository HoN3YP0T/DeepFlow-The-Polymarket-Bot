"""Process entrypoint.

Starts the orchestrator and the dashboard API together, and installs signal
handlers so a shutdown is graceful: stop opening new risk, drain, then
disconnect. A hard kill during an in-flight submission is precisely how an
uncertain execution is created.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

import uvicorn

from deepflow.api.app import create_app
from deepflow.config.settings import Settings, get_settings
from deepflow.core.errors import ConfigurationError
from deepflow.core.logging import configure_logging, get_logger
from deepflow.pipeline.orchestrator import Orchestrator

log = get_logger(__name__)

#: How long to wait for the API to drain before stopping the orchestrator.
#:
#: Short: the dashboard holds no positions and owes nobody a response worth delaying a
#: shutdown for. The orchestrator's own drain is the one that matters.
API_SHUTDOWN_SECONDS = 5.0


async def run(settings: Settings) -> int:
    """Run the orchestrator and the dashboard API together, until a signal arrives.

    **One process, and that is a requirement rather than a convenience.** The dashboard's
    controls act on the orchestrator's own breaker registry and risk engine; run as two
    processes they would trip a second registry that nothing consults -- a switch wired to
    nothing. This docstring claimed to start both for some time while ``run`` started only
    the orchestrator and ``make api`` started a dashboard whose ``app.state.orchestrator``
    was ``None``.

    The API is started **after** the orchestrator, so the dashboard never serves a window
    onto a system that has not finished reconciling. It is stopped first for the mirror
    reason: an operator should lose the buttons before the thing they act on goes away.
    """
    orchestrator = Orchestrator(settings=settings)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("deepflow.starting", mode=str(settings.mode))
    api: asyncio.Task[None] | None = None
    server: uvicorn.Server | None = None
    try:
        await orchestrator.start()
        server = _build_server(settings, orchestrator)
        api = asyncio.create_task(server.serve(), name="dashboard-api")
        log.info("deepflow.dashboard", url=f"http://{settings.api.host}:{settings.api.port}")
        await stop.wait()
    finally:
        log.info("deepflow.stopping")
        if server is not None:
            server.should_exit = True
        if api is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(api, timeout=API_SHUTDOWN_SECONDS)
        await orchestrator.stop()
    return 0


def _build_server(settings: Settings, orchestrator: Orchestrator) -> uvicorn.Server:
    """A uvicorn server sharing this event loop.

    ``Server.serve()`` as a task rather than ``uvicorn.run``, which would install its own
    loop and signal handlers and fight the ones above for the same SIGINT.
    """
    app = create_app(settings, orchestrator=orchestrator)
    return uvicorn.Server(
        uvicorn.Config(
            app,
            host=settings.api.host,
            port=settings.api.port,
            log_config=None,
            # Our own structlog configuration already emits request-level detail; letting
            # uvicorn install its handlers gives every line twice in two formats.
            access_log=False,
            lifespan="on",
        )
    )


def main() -> int:
    """CLI entrypoint.

    Configuration is validated before anything connects, so a misconfigured
    LIVE run fails at startup rather than after the first signal.
    """
    try:
        settings = get_settings()
    except ConfigurationError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    configure_logging(level=settings.log_level, fmt=settings.log_format)

    try:
        return asyncio.run(run(settings))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
