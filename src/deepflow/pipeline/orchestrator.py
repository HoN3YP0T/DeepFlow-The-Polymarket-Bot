"""Top-level runtime wiring.

Owns the long-lived asyncio tasks and the shutdown path. Composition happens
here so no module below has to know how the system is assembled.

Shutdown ordering matters: stop taking on new risk first, then drain, then
disconnect. Tearing down the stream while an order is in flight manufactures
exactly the uncertain-execution state the rest of the system works to avoid.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from deepflow.config.settings import Settings
from deepflow.core.logging import get_logger

log = get_logger(__name__)


@dataclass(slots=True)
class Orchestrator:
    """Runs the pipeline.

    Task inventory:
    * discovery sweep        -- slow poll, classify and validate
    * market stream          -- book/price updates for monitored tokens
    * sports stream          -- live game state
    * crypto price stream    -- reference spot for BTC markets
    * user stream            -- own fills (LIVE/SHADOW only)
    * smart-money poll       -- Data API wallet activity
    * signal loop            -- probability, EV, safety gate, execution
    * position manager       -- exit reevaluation for open positions
    * health monitor         -- breakers, freshness, reconciliation triggers
    """

    settings: Settings
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _stopping: asyncio.Event = field(default_factory=asyncio.Event)

    async def start(self) -> None:
        """Reconcile, then bring up the task set.

        Reconciliation runs before anything else and blocks the start on
        failure: section 20 requires trading to halt when local state and venue
        state disagree, and startup is the one moment we are guaranteed to be
        able to check cheaply.
        """
        raise NotImplementedError("Orchestrator.start")

    async def stop(self) -> None:
        """Graceful shutdown: halt entries, drain, cancel tasks, disconnect."""
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    @property
    def is_running(self) -> bool:
        return bool(self._tasks) and not self._stopping.is_set()
