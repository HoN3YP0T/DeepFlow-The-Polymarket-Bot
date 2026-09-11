"""Process entrypoint.

Starts the orchestrator and the dashboard API together, and installs signal
handlers so a shutdown is graceful: stop opening new risk, drain, then
disconnect. A hard kill during an in-flight submission is precisely how an
uncertain execution is created.
"""

from __future__ import annotations

import asyncio
import signal
import sys

from deepflow.config.settings import Settings, get_settings
from deepflow.core.errors import ConfigurationError
from deepflow.core.logging import configure_logging, get_logger
from deepflow.pipeline.orchestrator import Orchestrator

log = get_logger(__name__)


async def run(settings: Settings) -> int:
    """Run until a shutdown signal arrives."""
    orchestrator = Orchestrator(settings=settings)
    stop = asyncio.Event()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    log.info("deepflow.starting", mode=str(settings.mode))
    try:
        await orchestrator.start()
        await stop.wait()
    finally:
        log.info("deepflow.stopping")
        await orchestrator.stop()
    return 0


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
