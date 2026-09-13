"""Top-level runtime wiring.

Owns the long-lived asyncio tasks and the shutdown path. Composition happens
here so no module below has to know how the system is assembled.

Shutdown ordering matters: stop taking on new risk first, then drain, then
disconnect. Tearing down the stream while an order is in flight manufactures
exactly the uncertain-execution state the rest of the system works to avoid.

**Current scope.** Only the Phase 1 tasks are wired: discovery sweep, market
stream, snapshot persistence, health logging. The rest are listed in the task
inventory below and log once at startup as not yet wired, so what is running is
visible from the logs rather than inferred from which modules happen to exist.
This process therefore collects data and reports its own health. It does not
trade, and cannot -- nothing here can reach the execution adapter.

That is a useful thing to run long before it trades: the snapshot history a
backtest replays can only be gathered in real time, so starting collection early
is the one part of this build that cannot be caught up on later.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deepflow.adapters.persistence.engine import build_engine, build_session_factory
from deepflow.adapters.persistence.repositories import SqlUnitOfWork
from deepflow.adapters.polymarket.discovery import SdkMarketDiscovery
from deepflow.adapters.polymarket.games import GameLink, GammaGameLinks
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.adapters.polymarket.streams import PolymarketStreams
from deepflow.config.settings import Settings
from deepflow.core.clock import SystemClock
from deepflow.core.domain import Market, MarketSnapshot
from deepflow.core.enums import BreakerReason, DataQuality, RunMode
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId
from deepflow.engines.sports.rules import SportRegistry, category_for
from deepflow.pipeline.features import FeatureEngine
from deepflow.risk.breaker_supervisor import BreakerSupervisor
from deepflow.risk.circuit_breakers import CircuitBreakerRegistry

log = get_logger(__name__)

#: Tasks the design calls for that this process does not yet run. Logged at
#: startup rather than left implicit: "the bot is running" must not be mistaken
#: for "the bot is trading".
NOT_YET_WIRED = (
    "sports socket (in-play state comes from the REST sweep instead)",
    "crypto price stream",
    "user stream",
    "smart-money poll",
    "signal loop",
    "position manager",
    "reconciliation (needs an authenticated venue adapter)",
)

#: How often the health line is emitted.
HEALTH_INTERVAL_SECONDS = 30.0


@dataclass(slots=True)
class Orchestrator:
    """Runs the pipeline.

    Task inventory:
    * discovery sweep        -- slow poll, classify and validate
    * market stream          -- book/price updates for monitored tokens
    * live-game sweep        -- in-play fixtures, their state and their markets
    * crypto price stream    -- reference spot for BTC markets
    * user stream            -- own fills (LIVE/SHADOW only)
    * smart-money poll       -- Data API wallet activity
    * signal loop            -- probability, EV, safety gate, execution
    * position manager       -- exit reevaluation for open positions
    * health monitor         -- freshness and reconnects, fed to the breakers
    """

    settings: Settings
    _tasks: set[asyncio.Task[None]] = field(default_factory=set)
    _stopping: asyncio.Event = field(default_factory=asyncio.Event)

    _venue: PolymarketSession | None = None
    _engine: AsyncEngine | None = None
    _sessions: async_sessionmaker[AsyncSession] | None = None
    _streams: PolymarketStreams | None = None
    _features: FeatureEngine | None = None
    _games: GammaGameLinks | None = None
    _sports: SportRegistry | None = None
    _breakers: CircuitBreakerRegistry | None = None
    _supervisor: BreakerSupervisor | None = None
    _tracked: tuple[Market, ...] = ()
    _in_play: tuple[GameLink, ...] = ()
    _snapshots_written: int = 0

    async def start(self) -> None:
        """Reconcile, then bring up the task set.

        Reconciliation runs before anything else and blocks the start on
        failure: section 20 requires trading to halt when local state and venue
        state disagree, and startup is the one moment we are guaranteed to be
        able to check cheaply.

        It is skipped here only because nothing can have placed an order yet.
        Wiring the reconciler is Phase 5, and it must land *before* the execution
        adapter, not after -- a process that can trade but cannot establish what it
        already owns is the one configuration this design refuses.
        """
        if self._tasks:
            return

        self._stopping.clear()
        log.info(
            "orchestrator.starting",
            mode=str(self.settings.mode),
            not_yet_wired=list(NOT_YET_WIRED),
        )
        if self.settings.mode is RunMode.LIVE:
            # The interlock stands, but the reason has changed and saying the old one
            # would be a lie: the execution adapter, the reconciler and the breakers
            # all exist now. Two things still make LIVE indefensible, and neither is
            # about plumbing.
            raise RuntimeError(
                "refusing to start in LIVE mode. (1) No probability model exists, so "
                "the decision layer runs on injected estimates: the system can explain "
                "why it would not trade and cannot yet explain why it would. (2) No "
                "order has ever been submitted to this venue -- submit, cancel, "
                "cancel_all, the order heartbeat and the relayer are written from the "
                "published spec and unverified against it, and the first live "
                "submission is their first test. Reads are verified; writes are not"
            )

        self._venue = PolymarketSession(self.settings)
        await self._venue.start()

        self._engine = build_engine(self.settings)
        self._sessions = build_session_factory(self._engine)
        self._streams = PolymarketStreams(self._venue, self.settings)
        self._features = FeatureEngine(self.settings.thresholds, SystemClock())
        self._games = GammaGameLinks(self._venue)

        # The registry resolves a league code to its sport, and its first tier is
        # the venue's own league list. Loading it once at startup is what lets a
        # league the venue added later resolve without a release; a failure here
        # narrows coverage to the explicit map rather than stopping the process.
        self._sports = SportRegistry()
        await self._sports.load_from_venue(self._venue.public)

        # Breakers are constructed here even though nothing can trade yet: the
        # supervisor's staleness and reconnect conditions are about the data feed,
        # not about orders, and a feed problem is worth latching from the first run.
        clock = SystemClock()
        self._breakers = CircuitBreakerRegistry(self.settings.thresholds.breakers, clock)
        self._supervisor = BreakerSupervisor(
            registry=self._breakers,
            thresholds=self.settings.thresholds.breakers,
            execution=self.settings.thresholds.execution,
            limits=self.settings.thresholds.risk,
            clock=clock,
        )

        # Discovery runs once synchronously so the stream has a token set to open
        # with. Starting the stream first would mean subscribing to nothing and
        # reopening the socket immediately.
        await self._sweep_once()

        self._spawn(self._discovery_loop(), name="discovery")
        self._spawn(self._live_game_loop(), name="live-games")
        self._spawn(self._stream_loop(), name="market-stream")
        self._spawn(self._health_loop(), name="health")

    async def stop(self) -> None:
        """Graceful shutdown: halt entries, drain, cancel tasks, disconnect."""
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Streams before the venue session: the subscription is held by the
        # client, so disposing the client first leaves the pump reading a closed
        # socket and turns an orderly shutdown into a stack trace.
        if self._streams is not None:
            await self._streams.stop()
        if self._venue is not None:
            await self._venue.close()
        if self._engine is not None:
            await self._engine.dispose()
        log.info("orchestrator.stopped", snapshots_written=self._snapshots_written)

    @property
    def is_running(self) -> bool:
        return bool(self._tasks) and not self._stopping.is_set()

    @property
    def snapshots_written(self) -> int:
        return self._snapshots_written

    # --- Tasks ------------------------------------------------------------
    def _spawn(self, coro: Coroutine[Any, Any, None], *, name: str) -> None:
        task: asyncio.Task[None] = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task[None]) -> None:
        """Surface a task that died.

        A long-lived task ending on its own is never normal, and the default
        behaviour -- the exception sitting unretrieved on a discarded task -- is how
        a system keeps "running" with its data feed silently dead.
        """
        self._tasks.discard(task)
        if task.cancelled() or self._stopping.is_set():
            return
        exc = task.exception()
        if exc is not None:
            log.error("orchestrator.task_failed", task=task.get_name(), exc_info=exc)
        else:
            log.error("orchestrator.task_exited", task=task.get_name())

    async def _discovery_loop(self) -> None:
        """Re-read the market catalogue on a slow interval."""
        while not self._stopping.is_set():
            await asyncio.sleep(self.settings.discovery_interval_seconds)
            if self._stopping.is_set():
                return
            try:
                await self._sweep_once()
            except Exception:
                # A failed sweep is survivable: the existing token set keeps
                # streaming. Letting it kill the task would take the price feed
                # down over a metadata read.
                log.warning("discovery.sweep_failed", exc_info=True)

    async def _sweep_once(self) -> None:
        assert self._venue is not None and self._sessions is not None
        discovery = SdkMarketDiscovery(self._venue, self.settings)
        markets = await discovery.list_active_markets(limit=self.settings.max_tracked_markets)

        async with self._sessions() as session:
            uow = SqlUnitOfWork(session)
            for market in markets:
                await uow.markets.upsert(market)
            await uow.commit()

        self._tracked = tuple(markets)
        log.info("discovery.swept", markets=len(markets), tokens=len(self._token_ids()))

    def _token_ids(self) -> list[ClobTokenId]:
        return [outcome.token_id for market in self._tracked for outcome in market.outcomes]

    async def _stream_loop(self) -> None:
        """Fold the market stream and persist each snapshot."""
        assert self._streams is not None and self._features is not None
        tokens = self._token_ids()
        if not tokens:
            log.warning("stream.no_tokens", reason="discovery returned no tradeable markets")
            return

        async for snapshot in self._streams.subscribe_markets(tokens):
            quality = self._features.assess_snapshot(snapshot)
            if quality is DataQuality.INCONSISTENT:
                # Worth a line each time. An inconsistent book means our folded
                # state is wrong, which no amount of waiting fixes.
                log.warning("stream.inconsistent_snapshot", condition_id=str(snapshot.condition_id))
            if self.settings.persist_snapshots:
                await self._persist(snapshot)

    async def _persist(self, snapshot: MarketSnapshot) -> None:
        assert self._sessions is not None
        try:
            async with self._sessions() as session:
                uow = SqlUnitOfWork(session)
                self._snapshots_written += await uow.snapshots.record(snapshot)
                await uow.commit()
        except Exception:
            # Losing a snapshot row costs history, not correctness. Killing the
            # pump over it would cost the feed, so this is logged and skipped --
            # the database being down must not take the market data with it.
            #
            # It does trip the database breaker, though. A snapshot is history, but a
            # database that cannot be written to means positions, orders and the
            # journal are all diverging from reality, and the system can no longer
            # establish what it owns. Losing the row is survivable; trading on top of
            # it is not.
            log.warning("snapshot.persist_failed", exc_info=True)
            if self._supervisor is not None:
                self._supervisor.record_database_failure("snapshot persist failed")

    async def _live_game_loop(self) -> None:
        """Sweep in-play fixtures and read each one with its own sport's rules.

        This is the seam that was missing: the join
        (:mod:`deepflow.adapters.polymarket.games`) and the per-sport rules
        (:mod:`deepflow.engines.sports.rules`) were both written, tested and
        verified against the live venue, and neither was reachable from the running
        process -- so the pipeline had a probability layer it could not feed.

        A REST sweep rather than the socket, deliberately. The socket reports only
        what *changes* after connecting, so a process that has just started knows
        nothing about a game already at half time; one request returns every
        in-play fixture with its current state. The socket remains the lower-latency
        option once a fixture is known, which is why it stays on
        :data:`NOT_YET_WIRED` rather than being called done.
        """
        while not self._stopping.is_set():
            try:
                await self._sweep_live_games()
            except Exception:
                # Same reasoning as the discovery sweep: a failed metadata read
                # must not take down the price feed alongside it.
                log.warning("live_games.sweep_failed", exc_info=True)
            await asyncio.sleep(self.settings.live_game_interval_seconds)

    async def _sweep_live_games(self) -> None:
        assert self._games is not None and self._sports is not None
        links = await self._games.in_play()
        self._in_play = links

        modellable = 0
        unresolved: list[str] = []
        for link in links:
            kind = self._sports.sport_for(link)
            state = self._sports.parse(link)
            if state is not None and state.is_modellable:
                modellable += 1
            if category_for(kind) is None:
                # An unresolved league is recorded by name rather than defaulted:
                # letting it inherit OTHER_SPORTS would hand an unidentified sport
                # a real strategy's thresholds.
                unresolved.append(link.league_abbreviation)

        log.info(
            "live_games.swept",
            fixtures=len(links),
            modellable=modellable,
            tradeable_markets=sum(len(link.tradeable_markets) for link in links),
            unresolved_leagues=sorted(set(unresolved)),
        )

    async def _health_loop(self) -> None:
        """Report health, and let the supervisor act on it.

        The loop reports; the supervisor decides. Keeping the decision out of here
        is what stops trading policy accreting in the orchestrator -- this function
        knows how to observe a stream and nothing about what a reconnect rate means.

        Reconnects are fed in as *deltas*. The stream exposes a lifetime count and
        the supervisor measures a rate over a sliding hour, so handing it the total
        would replay the whole history into the window on every tick and trip on a
        long-running process that is currently fine.
        """
        seen_reconnects = 0
        while not self._stopping.is_set():
            await asyncio.sleep(HEALTH_INTERVAL_SECONDS)
            if self._stopping.is_set() or self._streams is None:
                return

            for _ in range(max(0, self._streams.reconnect_count - seen_reconnects)):
                if self._supervisor is not None:
                    self._supervisor.record_reconnect()
            seen_reconnects = self._streams.reconnect_count

            open_reasons: tuple[BreakerReason, ...] = ()
            if self._supervisor is not None:
                open_reasons = self._supervisor.evaluate(last_event_at=self._streams.last_event_at)

            log.info(
                "orchestrator.health",
                connected=self._streams.is_connected,
                reconnects=self._streams.reconnect_count,
                dropped=self._streams.dropped_events,
                last_event_at=str(self._streams.last_event_at),
                tracked_markets=len(self._tracked),
                in_play_fixtures=len(self._in_play),
                snapshots_written=self._snapshots_written,
                breakers_open=[str(reason) for reason in open_reasons],
                entries_allowed=self._breakers.entries_allowed() if self._breakers else None,
            )
