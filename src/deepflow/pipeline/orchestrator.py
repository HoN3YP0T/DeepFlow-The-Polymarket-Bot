"""Top-level runtime wiring.

Owns the long-lived asyncio tasks and the shutdown path. Composition happens
here so no module below has to know how the system is assembled.

Shutdown ordering matters: stop taking on new risk first, then drain, then
disconnect. Tearing down the stream while an order is in flight manufactures
exactly the uncertain-execution state the rest of the system works to avoid.

**Current scope.** Discovery sweep, market stream, snapshot persistence, live-game
sweep, health logging, the **reference-price feed**, and the **decision chain for
short-dated crypto markets** -- probability, EV, the safety gate and risk, journalled
in full. Everything still unwired is listed in the task inventory below and logged
once at startup, so what is running is visible from the logs rather than inferred
from which modules happen to exist.

This process decides and records; it does not trade, and cannot -- nothing here
constructs an execution adapter, and LIVE is refused before anything is built.

**Why the reference feed runs continuously and from the first moment.** A crypto
up/down market's strike is the Chainlink TWAP at the instant its window opens, and
the venue publishes that number nowhere (§77). A process that subscribes when it
notices the market has already missed it, permanently -- there is no later price that
substitutes, because the strike is one instant's value. So the feed is subscribed at
startup for every asset the venue runs the cadence on, independently of which markets
discovery happens to have found. It is the one input here that cannot be caught up on.

That is a useful thing to run long before it trades: the snapshot history a
backtest replays can only be gathered in real time, so starting collection early
is the one part of this build that cannot be caught up on later.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Coroutine, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import Any, Protocol

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from deepflow.adapters.persistence.engine import build_engine, build_session_factory
from deepflow.adapters.persistence.repositories import SqlUnitOfWork
from deepflow.adapters.polymarket.discovery import SdkMarketDiscovery
from deepflow.adapters.polymarket.games import GameLink, GammaGameLinks
from deepflow.adapters.polymarket.resolutions import PolymarketResolutions
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.adapters.polymarket.streams import PolymarketStreams
from deepflow.config.settings import Settings
from deepflow.config.thresholds import ProbabilityBand
from deepflow.core.clock import SystemClock
from deepflow.core.domain import (
    BaseRate,
    Classification,
    Market,
    MarketSnapshot,
    Prediction,
    ProbabilityEstimate,
    ResolutionCriteria,
    Signal,
)
from deepflow.core.enums import (
    BreakerReason,
    DataQuality,
    MarketCategory,
    RunMode,
    SignalAction,
)
from deepflow.core.logging import get_logger
from deepflow.core.state_machine import MarketState
from deepflow.core.types import ClobTokenId, ConditionId, SignalId
from deepflow.engines.calibration import IsotonicCalibrator
from deepflow.engines.crypto.btc_5m import TWAP_WINDOW_SECONDS, Btc5mEngine
from deepflow.engines.crypto.reference import TwapReference
from deepflow.engines.ev import EvEngine
from deepflow.engines.event_driven import EventDrivenEngine
from deepflow.engines.geopolitics.engine import GeopoliticalEngine
from deepflow.engines.geopolitics.events import EventPipeline
from deepflow.engines.politics.political import PoliticalEngine
from deepflow.engines.registry import EngineRegistry
from deepflow.engines.sports.football import FootballEngine
from deepflow.engines.sports.live_state import MatchStateStore
from deepflow.engines.sports.rules import SportRegistry, category_for
from deepflow.engines.sports.rules.base import MatchState
from deepflow.journal.recorder import JournalRecorder
from deepflow.pipeline.classifier import MarketClassifier
from deepflow.pipeline.features import FeatureEngine
from deepflow.pipeline.resolution import ResolutionValidator
from deepflow.pipeline.settlement import SettlementRecorder
from deepflow.risk.breaker_supervisor import BreakerSupervisor
from deepflow.risk.circuit_breakers import CircuitBreakerRegistry
from deepflow.risk.exposure import ExposureTracker
from deepflow.risk.limits import BankrollState, RiskEngine
from deepflow.risk.safety_gate import GateContext, SafetyGate, default_gate

log = get_logger(__name__)

#: Tasks the design calls for that this process does not yet run. Logged at
#: startup rather than left implicit: "the bot is running" must not be mistaken
#: for "the bot is trading".
NOT_YET_WIRED = (
    "sports socket (in-play state comes from the REST sweep instead)",
    "user stream",
    "smart-money poll",
    "execution (decisions are journalled, never submitted)",
    "position manager (no positions can be opened yet)",
    "reconciliation (nothing has placed an order)",
    "probability for every category except short-dated crypto",
)

#: Symbols the reference feed subscribes to: **all of them**.
#:
#: An empty tuple means no filter, and the venue then sends every symbol it publishes —
#: measured as 8 (bnb, btc, doge, eth, hype, sol, xrp, zec), exactly matching the assets it
#: runs the up/down cadence on. Subscribing to a named list instead was wrong about three
#: of eight (§79), and would have silently abstained on the assets it missed.
#:
#: The whole set rather than what discovery found, because the strike must already be in
#: hand when a window opens; a market noticed mid-window can never be priced (§77).
REFERENCE_SYMBOLS: tuple[str, ...] = ()

#: How often the health line is emitted.
HEALTH_INTERVAL_SECONDS = 30.0


#: The execution-quality fields a category's thresholds must expose for the gate.
#:
#: A structural type rather than a base class, because `Btc5mThresholds` and
#: `StrategyThresholds` are unrelated models that happen to answer the same four questions,
#: and making one inherit the other would imply a relationship that does not hold.
class ExecutionQualityLimits(Protocol):
    # Read-only properties rather than plain attributes: a mutable attribute in a Protocol
    # is invariant, and these models are frozen, so only the read-only form matches them.
    @property
    def max_data_age_seconds(self) -> float: ...
    @property
    def max_spread_bps(self) -> Decimal: ...
    @property
    def max_slippage_bps(self) -> Decimal: ...
    @property
    def min_liquidity_usdc(self) -> Decimal: ...
    @property
    def candidate_band(self) -> ProbabilityBand: ...


#: Categories the event-driven engines claim, and which share one set of measured
#: execution-quality limits.
EVENT_CATEGORIES = frozenset(
    {
        MarketCategory.POLITICS,
        MarketCategory.GEOPOLITICS,
        MarketCategory.WAR_CONFLICT,
        MarketCategory.CEASEFIRE,
        MarketCategory.MILITARY_DIPLOMATIC,
    }
)

#: How long to wait before resubscribing a dropped reference feed.
#:
#: Short, because every second without it is a second of TWAP history the process cannot
#: recover, and a market whose window opens inside the gap is unpriceable for its whole
#: life (§77).
#: Fewest seconds between two stored predictions for the same outcome token.
#:
#: A statistical guard, not a write-volume one. An engine re-estimates on every book
#: update, and a 5-minute crypto window sampled that way yields hundreds of rows whose
#: outcome is a single coin flip. Stored whole, they would make a curve fitted on a
#: handful of real outcomes look like one fitted on thousands -- and the tightest part
#: of that false confidence would sit in the 0.85-0.98 band this system trades.
#:
#: One minute keeps several genuinely different horizons per 5-minute market (the model
#: at 4 minutes out and at 1 minute out are different estimators) while cutting the
#: within-market correlation that inflates the sample. The fitter's ``MIN_MARKETS``
#: guards the same error from the other end.
#: How often the buffered snapshot writer drains to the database.
#:
#: One second. Long enough that a batch is thousands of rows rather than dozens --
#: which is the entire point, since the cost being avoided is the round trip, not the
#: insert -- and short enough that a crash loses about a second of history.
SNAPSHOT_FLUSH_SECONDS = 1.0

#: Fewest seconds between two persisted snapshots of the same market.
#:
#: A live book updates tens of times a second and a stored history does not need that:
#: entries run against a 3-10 second age budget, so anything finer is resolution nobody
#: reads. Writing it anyway is what broke the feed -- 2.7 million rows in a few hours,
#: a write per event, a consumer slower than the pump, and every overflow marking every
#: book gapped (§91).
#:
#: One second per market collapses that by more than an order of magnitude while leaving
#: the series finer than anything downstream consults. The same sampling idea as
#: ``PREDICTION_SAMPLE_SECONDS``, for a different reason: that one is about statistical
#: independence, this one is about throughput.
SNAPSHOT_SAMPLE_SECONDS = 1.0

#: Most rows one flush may write.
#:
#: Bounded so a flush is bounded work. Without a cap the first slow write leaves a
#: bigger buffer for the next one, which is slower again -- a death spiral that was
#: observed before this cap existed: the writer completed one batch of 4,056 rows and
#: never finished another while the buffer grew past 44,000.
SNAPSHOT_MAX_BATCH_ROWS = 5_000

#: Rows held before the oldest are dropped.
#:
#: Sized for several seconds of a busy feed, so an ordinary flush never touches the
#: bound and a database stall degrades gracefully instead of growing without limit.
#: Dropping the oldest is deliberate: the newest snapshot is the one a restarting
#: process would most want, and unbounded buffering trades a data problem for a
#: memory one.
SNAPSHOT_BUFFER_ROWS = 50_000

PREDICTION_SAMPLE_SECONDS = 60.0

REFERENCE_RETRY_SECONDS = 2.0


def _signal_id(condition_id: ConditionId, token_id: ClobTokenId, sequence: int) -> SignalId:
    """A short, deterministic signal id that fits the journal's column.

    ``journal.signal_id`` is ``varchar(64)`` and a condition id alone is 66 characters, so
    the obvious ``f"{condition}:{token}:{n}"`` overflowed it. Every decision of a ten-minute
    run then failed to insert — silently, because the recorder swallows write failures by
    design, so the health line reported 3,642 decisions while the table gained none (§83).

    A 12-hex digest of the market and token, plus the sequence: stable for the same
    market+outcome across restarts, unique per decision, and 21 characters.
    """
    digest = sha256(f"{condition_id}:{token_id}".encode()).hexdigest()[:12]
    return SignalId(f"{digest}-{sequence}")


class _SessionScopedJournal:
    """A :class:`JournalRepository` that opens its own session per write.

    The unit of work exists to commit a fill and its reasoning together, and there is no
    fill here to group with -- so a decision row gets its own short transaction rather
    than being held open across the stream loop. Holding one would mean a long-lived
    transaction on a connection the snapshot writer also needs.
    """

    def __init__(self, sessions: async_sessionmaker[AsyncSession], clock: SystemClock) -> None:
        self._sessions = sessions
        self._clock = clock

    async def record_signal(self, signal: Signal) -> None:
        async with self._sessions() as session:
            uow = SqlUnitOfWork(session, self._clock)
            await uow.journal.record_signal(signal)
            await uow.commit()

    async def record_decision(self, entry: dict[str, Any]) -> None:
        async with self._sessions() as session:
            uow = SqlUnitOfWork(session, self._clock)
            await uow.journal.record_decision(entry)
            await uow.commit()

    async def list_recent(self, *, limit: int = 100) -> Sequence[dict[str, Any]]:
        async with self._sessions() as session:
            return await SqlUnitOfWork(session, self._clock).journal.list_recent(limit=limit)


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
    _reference: TwapReference | None = None
    _engines: EngineRegistry | None = None
    _classifier: MarketClassifier | None = None
    _resolution: ResolutionValidator | None = None
    _ev: EvEngine | None = None
    _gate: SafetyGate | None = None
    _risk: RiskEngine | None = None
    _journal: JournalRecorder | None = None
    _decisions: int = 0
    _estimates: int = 0
    _unpriceable: int = 0
    _considered: int = 0
    _abstained: int = 0
    _context: dict[ConditionId, tuple[Classification, ResolutionCriteria]] = field(
        default_factory=dict
    )
    _events: EventPipeline | None = None
    _political: PoliticalEngine | None = None
    _geopolitical: GeopoliticalEngine | None = None
    _priors_applied: int = 0
    _verdicts: dict[ConditionId, tuple[Classification, ResolutionCriteria]] = field(
        default_factory=dict
    )
    _clock: SystemClock = field(default_factory=SystemClock)
    _tracked: tuple[Market, ...] = ()
    _settlement: SettlementRecorder | None = None
    _match_states: MatchStateStore | None = None
    _predictions_written: int = 0
    _calibrated_engines: int = 0
    _prediction_failures: int = 0
    _last_prediction: dict[ClobTokenId, datetime] = field(default_factory=dict)
    _in_play: tuple[GameLink, ...] = ()
    _snapshots_written: int = 0
    _snapshot_buffer: deque[MarketSnapshot] = field(
        default_factory=lambda: deque(maxlen=SNAPSHOT_BUFFER_ROWS)
    )
    _snapshot_batches_lost: int = 0
    _snapshots_evicted: int = 0
    _subscribed_tokens: set[ClobTokenId] = field(default_factory=set)
    _last_snapshot_persist: dict[ConditionId, datetime] = field(default_factory=dict)

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

        # The decision chain. Constructed together because a partial chain is worse
        # than none: an estimate with no gate behind it is a number nobody vetoed.
        self._reference = TwapReference()
        self._classifier = MarketClassifier(self.settings.thresholds)
        self._resolution = ResolutionValidator()
        self._ev = EvEngine(self.settings.thresholds)
        self._gate = default_gate()
        # No position repository is passed: nothing can open a position yet, so the
        # tracker starts empty rather than being handed a store it would read as
        # authoritative. It is wired the moment execution is.
        self._risk = RiskEngine(self.settings.thresholds.risk, ExposureTracker())
        # Without this every stake is zero, so every sized trade is unfillable and no
        # decision is ever reachable: a measured run produced 23,249 estimates and zero
        # journal rows (§82). In LIVE the balance would come from the venue instead, which
        # is one more reason LIVE is refused here.
        self._risk.update_bankroll(
            BankrollState(
                balance_usdc=self.settings.paper_bankroll_usdc,
                peak_balance_usdc=self.settings.paper_bankroll_usdc,
            )
        )
        self._engines = EngineRegistry()
        self._engines.register(
            Btc5mEngine(self.settings.thresholds.btc_5m, reference=self._reference, clock=clock)
        )

        # Politics and geopolitics, which are the bulk of the venue: 98 and 2 of the 100
        # markets a general sweep returns. They were discovered, streamed and snapshotted
        # while no engine claimed them, so every one was dropped from the decision context —
        # the `games.py` failure again, on the largest category there is.
        #
        # Both share one pipeline: corroboration is counted across a window of claims, and
        # two pipelines would each see half the reports and neither would reach the
        # two-publisher bar.
        self._events = EventPipeline(self.settings.thresholds.geopolitics, clock=clock)
        # Football reads live fixture state the sweep collects, the same shape as the
        # crypto model reading the TWAP series: an engine is handed a market, a book and a
        # token id, and none of those carry a score.
        self._match_states = MatchStateStore(clock)
        self._engines.register(
            FootballEngine(self.settings.thresholds.sports.football, states=self._match_states)
        )

        self._political = PoliticalEngine(self._events)
        self._geopolitical = GeopoliticalEngine(self._events, self.settings.thresholds.geopolitics)
        self._engines.register(self._political)
        self._engines.register(self._geopolitical)
        await self._install_calibrators()

        self._journal = JournalRecorder(
            repository=_SessionScopedJournal(self._sessions, clock),
            clock=clock,
            mode=self.settings.mode,
        )

        # The reference feed starts *before* discovery, and that ordering is the whole
        # point of the task: a window that opens while we are still reading the market
        # catalogue is a window whose strike we have missed for good (§77).
        self._spawn(self._reference_loop(), name="reference-feed")

        # Discovery runs once synchronously so the stream has a token set to open
        # with. Starting the stream first would mean subscribing to nothing and
        # reopening the socket immediately.
        await self._sweep_once()

        self._spawn(self._discovery_loop(), name="discovery")
        self._spawn(self._live_game_loop(), name="live-games")
        self._spawn(self._stream_loop(), name="market-stream")
        self._spawn(self._health_loop(), name="health")
        self._spawn(self._snapshot_writer(), name="snapshot-writer")

        # Settlement is what makes calibration possible: a prediction nobody scored is
        # half a sample, and until this loop existed the system stored only that half
        # (§90). It is spawned last because nothing else waits on it.
        self._settlement = SettlementRecorder(
            self._sessions, PolymarketResolutions(self._venue, clock), clock=clock
        )
        self._spawn(self._settlement.run_forever(), name="settlement")

    async def stop(self) -> None:
        """Graceful shutdown: halt entries, drain, cancel tasks, disconnect."""
        self._stopping.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

        # Drain what the writer task was holding when it was cancelled. Skipping this
        # would silently discard up to a flush interval of history on every clean stop,
        # which is the kind of loss that only shows up as a gap in a backtest months
        # later.
        await self._flush_snapshots()

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

    def rebuild_decision_context(self) -> None:
        """Re-derive which markets an engine can price, now rather than at the next sweep.

        Public so the dashboard can make a strategy toggle take effect immediately: an
        operator who disables an engine and watches the priceable count stay put will
        reasonably conclude the button did nothing.
        """
        if self._classifier is not None and self._engines is not None:
            self._rebuild_decision_context()

    @property
    def counters(self) -> dict[str, int]:
        """The decision-chain counters the health line reports.

        Exposed as one dict rather than seven properties because they are only ever
        meaningful together: "estimates" without "decisions" says the model spoke and
        nothing acted, and "decisions" without "journal_failures" once meant 3,642 rows
        that were never written (§83).
        """
        return {
            "considered": self._considered,
            "abstained": self._abstained,
            "estimates": self._estimates,
            "decisions": self._decisions,
            "unpriceable": self._unpriceable,
            "predictions": self._predictions_written,
            "prediction_failures": self._prediction_failures,
            "journal_failures": self._journal.write_failures if self._journal else 0,
            "snapshots_written": self._snapshots_written,
            "snapshots_evicted": self._snapshots_evicted,
            "priceable_markets": len(self._context),
            "priors_applied": self._priors_applied,
            "settled": self._settlement.recorded if self._settlement else 0,
            "awaiting_settlement": self._settlement.pending if self._settlement else 0,
        }

    @property
    def streams(self) -> PolymarketStreams | None:
        return self._streams

    @property
    def reference(self) -> TwapReference | None:
        return self._reference

    @property
    def in_play(self) -> tuple[GameLink, ...]:
        return self._in_play

    @property
    def sessions(self) -> async_sessionmaker[AsyncSession] | None:
        """The database session factory, for the dashboard to read and audit through.

        Shared rather than re-created: a second engine against the same database would
        double the connection pool and make "who holds a connection" unanswerable when one
        of the two is leaking.
        """
        return self._sessions

    @property
    def breakers(self) -> CircuitBreakerRegistry | None:
        """The halt authority, so a dashboard control trips the same one the trading path
        consults. A second registry would be a switch wired to nothing."""
        return self._breakers

    @property
    def risk(self) -> RiskEngine | None:
        return self._risk

    @property
    def engines(self) -> EngineRegistry | None:
        return self._engines

    @property
    def tracked_markets(self) -> tuple[Market, ...]:
        return self._tracked

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

        # The up/down markets, fetched separately because the general sweep cannot see
        # them: they are listed ~24h early and carry no volume until their final minutes,
        # so a liquidity-ranked page of 100 returned zero of them (§80). Fetched through
        # events so the contest window travels with each market.
        short_dated: tuple[Market, ...] = ()
        try:
            short_dated = tuple(
                await discovery.list_short_dated_crypto(limit=self.settings.max_tracked_markets)
            )
        except Exception:
            # Survivable and worth saying: the general sweep still succeeded, so the
            # process keeps streaming everything else rather than losing the feed over one
            # extra request.
            log.warning("discovery.short_dated_failed", exc_info=True)

        async with self._sessions() as session:
            uow = SqlUnitOfWork(session)
            for market in short_dated:
                await uow.markets.upsert(market)
            await uow.commit()

        self._tracked = tuple(markets) + short_dated
        self._rebuild_decision_context()
        await self._persist_verdicts()
        log.info(
            "discovery.swept",
            markets=len(markets),
            short_dated=len(short_dated),
            tokens=len(self._token_ids()),
        )

    def _token_ids(self) -> list[ClobTokenId]:
        return [outcome.token_id for market in self._tracked for outcome in market.outcomes]

    async def _stream_loop(self) -> None:
        """Fold the market stream, and re-open it when new markets appear.

        **The subscription used to be fixed at startup**, and that quietly capped what
        the whole pipeline could see. ``PolymarketStreams.start`` is idempotent -- the
        SDK subscribes per connection, so changing the token set means reopening -- and
        this loop called ``subscribe_markets`` exactly once, with whatever the first
        sweep had found. Everything discovered afterwards was tracked, classified,
        persisted and never streamed.

        Measured across two sweeps: **12 markets newly tracked, 0 of them streamed.**
        For a venue that lists a fresh five-minute crypto window every five minutes,
        that means the crypto model's market supply decays to nothing shortly after
        startup, and no sports fixture discovered mid-run is ever priced.

        Re-opened only when tokens appear that we are *not* subscribed to. Markets
        dropping out need no reopen -- they simply go quiet -- and reopening on every
        sweep would churn the socket every five minutes for no gain. A reopen is not a
        reconnect and is deliberately not counted as one: the breakers watch for a feed
        that keeps dropping, and a planned resubscription is not evidence of that.
        """
        assert self._streams is not None and self._features is not None

        while not self._stopping.is_set():
            tokens = self._token_ids()
            if not tokens:
                log.warning("stream.no_tokens", reason="discovery returned no tradeable markets")
                return

            self._subscribed_tokens = set(tokens)
            log.info("stream.subscribing", tokens=len(tokens))
            stream = self._streams.subscribe_markets(tokens)
            try:
                async for snapshot in stream:
                    quality = self._features.assess_snapshot(snapshot)
                    if quality is DataQuality.INCONSISTENT:
                        # Worth a line each time. An inconsistent book means our folded
                        # state is wrong, which no amount of waiting fixes.
                        log.warning(
                            "stream.inconsistent_snapshot",
                            condition_id=str(snapshot.condition_id),
                        )
                    if self.settings.persist_snapshots:
                        self._buffer_snapshot(snapshot)
                    try:
                        await self._consider(snapshot)
                    except Exception:
                        # A failure in the decision chain must not take the feed with
                        # it. The snapshot history is the one thing that cannot be
                        # collected later, and a bug in sizing or the gate is no reason
                        # to stop gathering it.
                        log.warning("signal.consider_failed", exc_info=True)

                    if self._stopping.is_set() or self._missing_tokens():
                        break
            finally:
                # Closed explicitly rather than left to the garbage collector: an async
                # generator abandoned mid-iteration keeps the queue consumer alive, and
                # the next subscription would then race the old one for events.
                await stream.aclose()

            if self._stopping.is_set():
                return

            # The pump holds the token set, so a new set needs a new pump.
            await self._streams.stop()

    def _missing_tokens(self) -> bool:
        """Whether discovery has found tokens this subscription does not carry.

        Only additions trigger a reopen. A market that has dropped out of the tracked
        set costs nothing by staying subscribed -- it simply stops updating -- while
        reopening for removals would churn the socket on every sweep.
        """
        return bool(set(self._token_ids()) - self._subscribed_tokens)

    def _buffer_snapshot(self, snapshot: MarketSnapshot) -> None:
        """Queue a snapshot for the writer, at most one per market per second.

        Two decisions, both forced by measurement rather than taste.

        **Buffered, never written inline.** A session round trip per book update made
        this loop slower than the pump. The cost was not just lost history: each queue
        overflow marks *every* book gapped, so the feed degraded its own data and the
        entry gate then refused all of it -- 97% of 2.7 million rows came out DEGRADED
        (§91).

        **Sampled, not complete.** Even batched, a row per book update is more history
        than anything downstream reads: entries run against a 3-10 second age budget.
        Dropping to one row per market per second is the difference between a write
        volume the database can absorb and one it cannot.
        """
        now = self._clock.now()
        last = self._last_snapshot_persist.get(snapshot.condition_id)
        if last is not None and (now - last).total_seconds() < SNAPSHOT_SAMPLE_SECONDS:
            return
        self._last_snapshot_persist[snapshot.condition_id] = now
        if len(self._snapshot_buffer) == self._snapshot_buffer.maxlen:
            # A deque at maxlen evicts silently. Counting it here is what stops this
            # becoming the next invisible loss -- the queue overflow it replaced went
            # unnoticed until it had cost 2.7 million degraded rows.
            self._snapshots_evicted += 1
        self._snapshot_buffer.append(snapshot)

    def _with_microstructure(self, snapshot: MarketSnapshot) -> MarketSnapshot:
        """Attach order-flow features to a snapshot about to be stored.

        ``FeatureEngine.compute`` -- Phase 3 item 10, built and tested -- was called by
        nothing on the running path. The consequence was measurable: **2,737,376 stored
        snapshots with zero non-null ``book_imbalance`` and zero non-null
        ``flow_imbalance``**, columns created for a measurement nobody was taking.

        Computed **in the writer task, never on the consumer path.** This was first put
        in the sampling step -- once per market per second, which sounded bounded -- and
        it immediately cost 53,630 dropped events, because ``compute`` walks every level
        of a book twice (the wide-band confirmation re-scans) and the books here carry
        30-150 levels a side. §81 a third time, self-inflicted: anything that touches
        book levels belongs off the path the fold runs on.

        Nothing reads these columns yet -- ``MicrostructureEngine`` and its
        ``FlowAssessment`` are still unwired, a Phase 4 item. What this buys is that the
        recorded history contains what its schema promises, which is the difference
        between a backtest being possible later and the data never having existed.
        """
        if self._features is None or not snapshot.books:
            return snapshot
        try:
            return snapshot.model_copy(
                update={"microstructure": self._features.compute(book=snapshot.books[0])}
            )
        except Exception:
            # A feature that cannot be measured must not cost the row it was attached
            # to: the prices are the part a backtest cannot do without.
            log.warning("snapshot.microstructure_failed", exc_info=True)
            return snapshot

    async def _snapshot_writer(self) -> None:
        """Drain the snapshot buffer to the database on a fixed cadence.

        Separated from the stream consumer so that database latency can never slow the
        fold. That coupling is what produced §91: with the write inline, 529,756 events
        were dropped in 90 seconds against **zero** with persistence disabled, and every
        drop marked every book gapped.

        A full buffer drops the **oldest** rows rather than blocking. Snapshot history
        is valuable and recoverable-by-waiting; the live fold is neither, so when the
        two compete the history loses. The count is reported on the health line so that
        "we are dropping history" cannot become invisible the way the queue overflow
        was.
        """
        while not self._stopping.is_set():
            await asyncio.sleep(SNAPSHOT_FLUSH_SECONDS)
            await self._flush_snapshots()

    async def _flush_snapshots(self) -> None:
        """Write at most one capped batch.

        Capped rather than "everything currently buffered": an unbounded flush makes
        each write slower than the last, which was observed as a writer that completed
        one batch and never finished another while the buffer grew past 44,000 rows.
        """
        if not self._snapshot_buffer or self._sessions is None:
            return

        batch: list[MarketSnapshot] = []
        while self._snapshot_buffer and len(batch) < SNAPSHOT_MAX_BATCH_ROWS:
            batch.append(self._with_microstructure(self._snapshot_buffer.popleft()))
        try:
            async with self._sessions() as session:
                uow = SqlUnitOfWork(session)
                self._snapshots_written += await uow.snapshots.record_many(batch)
                await uow.commit()
        except Exception:
            # Losing a batch costs history, not correctness -- the same reasoning as
            # the single-row path it replaced, and the same breaker trip, because a
            # database that cannot be written to means positions, orders and the
            # journal are all diverging from reality.
            self._snapshot_batches_lost += 1
            log.warning("snapshot.persist_failed", rows=len(batch), exc_info=True)
            if self._supervisor is not None:
                self._supervisor.record_database_failure("snapshot persist failed")

    async def _reference_loop(self) -> None:
        """Hold the Chainlink TWAP series every short-dated crypto market settles on.

        Runs for the life of the process. A gap here is not recoverable later: the strike
        of an up/down market is the value at one instant, published nowhere (§77), so a
        feed that reconnects after a window opened has permanently lost that market.

        A dropped subscription is retried rather than fatal, and the retry is logged and
        counted as a reconnect so the breakers see it -- a silently dead reference feed
        would leave the engine abstaining forever while every other signal looked healthy,
        which is indistinguishable from a quiet market.
        """
        assert self._streams is not None and self._reference is not None
        while not self._stopping.is_set():
            try:
                async for price in self._streams.subscribe_crypto_twap(
                    REFERENCE_SYMBOLS, window_seconds=TWAP_WINDOW_SECONDS
                ):
                    self._reference.observe(price)
                    if self._stopping.is_set():
                        return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("reference.feed_dropped", exc_info=True)
                if self._supervisor is not None:
                    self._supervisor.record_reconnect()
            if self._stopping.is_set():
                return
            await asyncio.sleep(REFERENCE_RETRY_SECONDS)

    def _rebuild_decision_context(self) -> None:
        """Classify and parse resolution **once per market**, not once per snapshot.

        Both are properties of the market and neither changes between book updates, but
        doing them inline cost the feed: with the chain running per snapshot the stream
        dropped **1.3 million events** in ten minutes while writing 312,000, because the
        consumer could not keep up with the pump (§81). Classification runs a keyword and
        tag scan and resolution parsing runs a set of regexes over prose -- per snapshot,
        for 172 markets, at the rate a live book moves.

        Only markets an engine actually claims are kept. That makes the per-snapshot path a
        dict lookup that misses for the 90% of tracked markets nothing can price, which is
        the cheapest possible answer for the common case.
        """
        assert self._classifier is not None and self._resolution is not None
        assert self._engines is not None

        context: dict[ConditionId, tuple[Classification, ResolutionCriteria]] = {}
        verdicts: dict[ConditionId, tuple[Classification, ResolutionCriteria]] = {}
        for market in self._tracked:
            classification = self._classifier.classify(market)
            resolution = self._resolution.validate(market)
            # Kept for **every** tracked market, so the database can record what we
            # decided about all of them. The context below keeps only the priceable
            # ones, which is a different question and a much smaller set.
            verdicts[market.condition_id] = (classification, resolution)
            if self._engines.resolve(classification) is None:
                continue
            context[market.condition_id] = (classification, resolution)
        self._context = context
        self._verdicts = verdicts
        self._apply_base_rates()
        log.info(
            "orchestrator.decision_context",
            priceable_markets=len(context),
            tracked=len(self._tracked),
            priors_applied=self._priors_applied,
        )

    async def _persist_verdicts(self) -> None:
        """Write what we decided about each market, not just what the venue says it is.

        Without this the ``markets`` table records only the venue's own fields: the
        process upserted every market and never wrote a category, a resolution verdict
        or a lifecycle state, so **398 rows sat at DISCOVERED / NOT_CHECKED / UNKNOWN**
        beside 161 properly classified ones that a verification script had written once,
        by hand, weeks earlier.

        That is the `games.py` shape again: ``DiscoveryService`` does all of this --
        lifecycle transitions, rejection journalling, the CLASSIFIED -> VALIDATED ->
        MONITORED edges -- is fully tested, and is constructed by nothing on the running
        path. Wiring that service properly means reconciling its own sweep with the
        orchestrator's two-part one (general plus short-dated), which is a larger change
        than this; what this does is stop the table being actively misleading in the
        meantime, using the classification the process has already computed.

        ``record_classification`` is deliberately separate from ``upsert``: a catalogue
        refresh must not reset our verdicts, which is why the two writes are distinct.
        """
        if self._sessions is None or not self._verdicts:
            return

        try:
            async with self._sessions() as session:
                uow = SqlUnitOfWork(session, self._clock)
                for condition_id, (classification, resolution) in self._verdicts.items():
                    await uow.markets.record_classification(
                        condition_id,
                        category=classification.category.value,
                        confidence=classification.confidence,
                        lifecycle_state=_lifecycle_for(classification, resolution).value,
                        resolution_validity=resolution.validity.value,
                    )
                await uow.commit()
        except Exception:
            # Same reasoning as every other write on this path: losing the verdicts
            # costs a record, and taking the feed down over it costs the trading loop.
            log.warning("discovery.verdicts_persist_failed", exc_info=True)

    def _apply_base_rates(self) -> None:
        """Hand each event-driven engine the operator priors for the markets it claims.

        Matched on **slug**, because a slug is the thing an operator can read off the market
        page; a condition id is 66 hex characters and not something anyone transcribes
        correctly. A prior naming a market that is not tracked is logged rather than ignored:
        a typo in a slug otherwise looks exactly like a market that has closed.

        Without priors both engines abstain on everything, which is correct rather than
        broken — see ``Settings.base_rates``.
        """
        configured = self.settings.base_rates
        if not configured or self._engines is None:
            return

        by_slug = {market.slug: market for market in self._tracked if market.slug}
        now = self._clock.now()
        applied = 0
        for slug, prior in configured.items():
            market = by_slug.get(slug)
            if market is None:
                log.warning("orchestrator.prior_market_not_tracked", slug=slug)
                continue
            engine = (
                self._engines.resolve(self._context[market.condition_id][0])
                if (market.condition_id in self._context)
                else None
            )
            if not isinstance(engine, EventDrivenEngine):
                log.warning(
                    "orchestrator.prior_for_unpriceable_market",
                    slug=slug,
                    reason="no event-driven engine claims this market",
                )
                continue
            engine.set_base_rate(
                market.condition_id,
                BaseRate(
                    probability=prior.probability,
                    uncertainty=prior.uncertainty,
                    source=prior.source,
                    # Stamped here rather than taken from configuration: a prior that
                    # self-reported its age could claim to be fresher than the process.
                    as_of=now,
                ),
            )
            applied += 1
        self._priors_applied = applied

    async def _consider(self, snapshot: MarketSnapshot) -> None:
        """Run one market's snapshot through probability, EV, the gate and risk.

        Journals every outcome that reached a verdict, including refusals. The trades
        taken are a biased sample of the opportunities seen, and a log that keeps only
        approvals can never say whether a gate is correctly protective or merely never
        satisfied.

        Silent on an **abstention**, deliberately. The BTC engine declines most snapshots
        it is offered -- an unobserved strike, a stale tick, a horizon inside the averaging
        window -- and journalling each one would bury the decisions that did happen under
        thousands of rows saying nothing happened. Abstentions are counted and reported on
        the health line instead.
        """
        assert self._engines is not None
        cached = self._context.get(snapshot.condition_id)
        if cached is None:
            # Not priceable by any engine, established at sweep time. The common case by
            # far, and deliberately the cheapest: one dict lookup.
            return
        classification, resolution = cached

        market = self._market_for(snapshot.condition_id)
        if market is None:
            return
        engine = self._engines.resolve(classification)
        if engine is None:
            return

        self._considered += 1
        estimate = await engine.estimate(
            market=market, snapshot=snapshot, token_id=self._entry_token(market)
        )
        if estimate is None:
            self._abstained += 1
            return
        self._estimates += 1
        await self._record_prediction(market, classification, estimate)
        await self._decide(market, snapshot, classification, resolution, estimate)

    async def _install_calibrators(self) -> None:
        """Give each engine the calibration curve an operator has marked active.

        Only fits explicitly activated are loaded. A fit sitting in the table is a
        measurement; a fit marked active is a decision, and this system should not
        start applying a correction to every probability it produces because a
        fitting job happened to run overnight.

        A failure here leaves every engine on identity, which is the uncalibrated
        behaviour the system has had all along -- so a database problem costs the
        correction, never the run.
        """
        assert self._engines is not None and self._sessions is not None
        try:
            async with self._sessions() as session:
                fits = await SqlUnitOfWork(session, self._clock).calibration.active_fits()
        except Exception:
            log.warning("calibration.load_failed", exc_info=True)
            return

        if not fits:
            log.info("calibration.none_active", engines=len(self._engines.engines))
            return

        for engine in self._engines.engines:
            payload = fits.get(engine.name)
            if payload is None:
                continue
            try:
                calibrator = IsotonicCalibrator.from_dict(payload)
            except (ValueError, KeyError, ArithmeticError):
                # A malformed stored fit must not silently become an identity that
                # looks calibrated; it is named here and the engine stays honest.
                log.warning("calibration.fit_unreadable", engine=engine.name, exc_info=True)
                continue
            installer = getattr(engine, "use_calibrator", None)
            if installer is None:
                log.warning("calibration.engine_not_calibratable", engine=engine.name)
                continue
            installer(calibrator)
            self._calibrated_engines += 1

    async def _record_prediction(
        self,
        market: Market,
        classification: Classification,
        estimate: ProbabilityEstimate,
    ) -> None:
        """Store an estimate so it can be scored once the market settles.

        Recorded for **every** estimate that reaches this point, not only the ones that
        became signals. Calibrating on the traded subset would fit the curve to the
        region where this system already believed it had an edge -- the tail above 0.85
        -- and leave it blind everywhere else, which is the region a fit is supposed to
        correct.

        **Sampled at one row per token per minute**, and that is a statistical decision
        rather than a write-volume one. A 5-minute crypto window re-estimated on every
        book update yields hundreds of rows resolved by a single coin flip; stored
        whole, they would make a fit built on a handful of real outcomes look like one
        built on thousands. The fitter counts distinct markets for the same reason
        (``MIN_MARKETS``), so this is the second of two guards against the same error.

        A failure here is counted and swallowed. A prediction row is evidence for a
        future fit, not a safety mechanism, and losing one must not cost the decision
        that was about to be made on the estimate itself.
        """
        if self._sessions is None:
            return

        now = self._clock.now()
        last = self._last_prediction.get(estimate.token_id)
        if last is not None and (now - last).total_seconds() < PREDICTION_SAMPLE_SECONDS:
            return
        self._last_prediction[estimate.token_id] = now

        # Horizon rather than market age: calibration is horizon-dependent, since a model
        # five seconds from settlement is a different estimator from the same model five
        # minutes out. `None` when the end is unknown -- never 0, which would read as
        # "settles now" and pool those rows with the sharpest predictions in the set.
        horizon: int | None = None
        if market.end_date is not None:
            horizon = max(int((market.end_date - now).total_seconds()), 0)

        try:
            async with self._sessions() as session:
                uow = SqlUnitOfWork(session, self._clock)
                await uow.predictions.record(
                    Prediction(
                        engine=estimate.engine,
                        category=classification.category,
                        condition_id=market.condition_id,
                        token_id=estimate.token_id,
                        model_probability=estimate.model_probability,
                        calibrated_probability=estimate.calibrated_probability,
                        uncertainty=estimate.uncertainty,
                        predicted_at=now,
                        horizon_seconds=horizon,
                    )
                )
                await uow.commit()
            self._predictions_written += 1
        except Exception:
            self._prediction_failures += 1
            log.warning("prediction.persist_failed", exc_info=True)

    async def _decide(
        self,
        market: Market,
        snapshot: MarketSnapshot,
        classification: Classification,
        resolution: ResolutionCriteria,
        estimate: ProbabilityEstimate,
    ) -> None:
        """Size, price and vet a candidate, then record the verdict.

        The order is fixed and each step can only refuse: risk sizes the trade, EV prices
        it *at that size*, and the gate examines the whole picture. Sizing first because an
        EV figure is meaningless without a size -- the cost of crossing the book depends on
        how much of it you take -- and the gate last because it is the only component that
        sees every input at once.

        Two paths leave no journal row, and both are correct rather than convenient:

        * **No ask.** There is no price to buy at, so there is no candidate.
        * **No EV assessment.** ``EvEngine.assess`` returns ``None`` when the book cannot
          support the size, and :class:`Signal` requires a priced assessment -- by
          construction, not by omission. Fabricating one to get a row written would put an
          invented cost into the record that later analysis would read as measured.

        Both are counted and surfaced on the health line instead, because "the gate
        rejected nothing today" and "nothing was ever priceable" are very different
        states.
        """
        assert self._risk is not None and self._ev is not None and self._gate is not None
        assert self._resolution is not None and self._journal is not None

        limits = self._limits_for(classification.category)
        if limits is None:
            # Fail closed on an unknown category rather than lending it the crypto limits.
            # Those limits are measured for a market with a 0.01 tick and a 300-second life;
            # applying them to a football market would be a number that looks calibrated and
            # is not.
            log.warning("signal.no_limits_for_category", category=str(classification.category))
            return

        book = snapshot.book_for(estimate.token_id)
        price = book.best_ask if book is not None else None
        if price is None or price <= 0:
            self._unpriceable += 1
            return

        verdict = self._risk.approve(
            probability=estimate.calibrated_probability,
            price=price,
            uncertainty=estimate.uncertainty,
            condition_id=market.condition_id,
        )
        # Shares from the stake at the price we would pay. A refused verdict still has a
        # size of zero, which ``assess`` will correctly decline to price.
        stake = verdict.sizing.stake_usdc if verdict.sizing else Decimal(0)
        shares = stake / price if stake > 0 else Decimal(0)

        assessment = self._ev.assess(
            estimate=estimate,
            snapshot=snapshot,
            token_id=estimate.token_id,
            size_shares=shares,
            market=market,
        )
        if assessment is None:
            self._unpriceable += 1
            log.info(
                "signal.unpriceable",
                condition_id=str(market.condition_id),
                reason="book cannot support the sized trade",
                stake=str(stake),
            )
            return

        decision = self._gate.evaluate(
            GateContext(
                now=self._clock.now(),
                market=market,
                classification=classification,
                resolution=resolution,
                snapshot=snapshot,
                estimate=estimate,
                ev=assessment,
                risk=verdict,
                capital_available_usdc=self._risk.available_capital(),
                stake_usdc=stake,
                execution_healthy=self._breakers is None or not self._breakers.open_reasons,
                # Six checks previously failed for want of an input rather than on merit,
                # which is the gate failing closed exactly as designed — and a gate that
                # refuses because nobody told it the limits is not examining the trade. The
                # fix is to supply them, never to soften the checks (hard rule 2).
                max_data_age_seconds=limits.max_data_age_seconds,
                max_spread_bps=limits.max_spread_bps,
                max_slippage_bps=limits.max_slippage_bps,
                min_liquidity_usdc=limits.min_liquidity_usdc,
                # The band the strategy actually trades, which until 2026-09-14 was
                # configured in seven places and read in none.
                candidate_band=limits.candidate_band,
                # A known fact, not an assumption: this process constructs no execution
                # adapter, so no order of ours can exist to duplicate. It becomes a real
                # lookup the moment execution is wired.
                duplicate_order_exists=False,
            )
        )

        self._decisions += 1
        signal = Signal(
            signal_id=_signal_id(market.condition_id, estimate.token_id, self._decisions),
            condition_id=market.condition_id,
            token_id=estimate.token_id,
            action=SignalAction.BUY,
            category=classification.category,
            probability=estimate,
            ev=assessment,
            target_price=price,
            generated_at=self._clock.now(),
            rationale=f"{estimate.engine}: {verdict.reason}",
        )
        context = {"mode": str(self.settings.mode), "engine": estimate.engine}

        if decision.approved and verdict.approved:
            # Recorded as an entry and **not submitted**: no execution adapter is
            # constructed in this process, so an approval is a paper decision by
            # construction rather than by a flag someone could flip.
            await self._journal.record_entry(signal, gate=decision, context=context)
            log.info(
                "signal.approved_not_submitted",
                condition_id=str(market.condition_id),
                model_probability=str(estimate.calibrated_probability),
                price=str(price),
                net_ev=str(assessment.net_ev),
                stake=str(stake),
            )
        else:
            await self._journal.record_rejection(signal, gate=decision, context=context)

    def _limits_for(self, category: MarketCategory) -> ExecutionQualityLimits | None:
        """Execution-quality limits for a category, or ``None`` when none are defined.

        Explicit per category because the numbers are not transferable: the crypto limits
        are measured against a 0.01 tick and a 300-second life, and the sports ones against
        books that stay two-sided for hours. A default that quietly lent one set to the other
        would be the most plausible-looking way to mis-gate a trade.
        """
        thresholds = self.settings.thresholds
        if category is MarketCategory.BTC_5M:
            return thresholds.btc_5m
        if category in EVENT_CATEGORIES:
            return thresholds.event_markets
        by_sport = {
            MarketCategory.FOOTBALL: thresholds.sports.football,
            MarketCategory.CRICKET: thresholds.sports.cricket,
            MarketCategory.TENNIS: thresholds.sports.tennis,
            MarketCategory.BADMINTON: thresholds.sports.badminton,
        }
        return by_sport.get(category)

    def _market_for(self, condition_id: ConditionId) -> Market | None:
        return next((m for m in self._tracked if m.condition_id == condition_id), None)

    @staticmethod
    def _entry_token(market: Market) -> ClobTokenId:
        """The outcome this system would buy.

        The first outcome, which on an up/down market is ``Up``. Named rather than inlined
        because choosing a token is a trading decision and not a lookup: the complement is
        a different trade at a different price, and a model that estimates one while the
        book is read for the other is the quietest available way to be wrong.
        """
        return market.outcomes[0].token_id

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
                self._observe_fixture(link, state)
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
            # Zero here while fixtures are in play means the join is broken rather than
            # that nothing is on -- the failure `games.py` sat in for two phases.
            markets_with_state=self._match_states.tracked() if self._match_states else 0,
        )

    def _observe_fixture(self, link: GameLink, state: MatchState) -> None:
        """Hand one fixture's state to every market built on it.

        Done at **sweep** time, not per snapshot. Parsing a fixture per book update is
        the §81 mistake: classification and resolution inline in the stream consumer
        dropped 1.3 million events in ten minutes while reporting itself healthy.

        A fixture with no named sides is skipped. A soccer result market is a three-way
        group whose three members are distinguished only by ``group_item_title`` matching
        a team name, so without the names the state is unusable and storing it would only
        let the engine get further before abstaining.
        """
        if self._match_states is None:
            return
        if not link.home_team or not link.away_team:
            log.info("live_games.fixture_unnamed", slug=link.slug)
            return
        self._match_states.observe(
            tuple(market.condition_id for market in link.tradeable_markets),
            state,
            home_team=link.home_team,
            away_team=link.away_team,
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
                # Next to the written count on purpose: a dropped batch is history
                # that is gone, and the queue overflow it replaced was invisible
                # until it had cost 2.7 million degraded rows.
                snapshot_batches_lost=self._snapshot_batches_lost,
                snapshots_evicted=self._snapshots_evicted,
                snapshot_buffer=len(self._snapshot_buffer),
                breakers_open=[str(reason) for reason in open_reasons],
                entries_allowed=self._breakers.entries_allowed() if self._breakers else None,
                # The three decision counters, together, because each alone misleads.
                # "estimates" without "decisions" says the model spoke and nothing acted;
                # "decisions" without "unpriceable" hides a book too thin to trade; and a
                # reference series of zero says the model cannot speak at all, whatever
                # the other numbers look like.
                reference_symbols=self._reference.symbols() if self._reference else 0,
                considered=self._considered,
                abstained=self._abstained,
                estimates=self._estimates,
                decisions=self._decisions,
                unpriceable=self._unpriceable,
                # Reported next to `decisions` on purpose: without it "decisions=3642" can
                # mean 3,642 rows or none, and a run once meant none (§83).
                journal_failures=self._journal.write_failures if self._journal else 0,
                # Calibration's raw material. `predictions` counts what was stored to be
                # scored later and `settled` what has been scored; the gap between them is
                # simply markets that have not ended yet, and a `settled` stuck at zero
                # while `predictions` climbs is the one shape that means the scoring join
                # is broken rather than merely waiting.
                predictions=self._predictions_written,
                prediction_failures=self._prediction_failures,
                calibrated_engines=self._calibrated_engines,
                settled=self._settlement.recorded if self._settlement else 0,
                awaiting_settlement=self._settlement.pending if self._settlement else 0,
            )


def _lifecycle_for(classification: Classification, resolution: ResolutionCriteria) -> MarketState:
    """The lifecycle state a market has actually reached.

    Three outcomes, and the distinction between the last two is the one worth keeping:

    * ``MARKET_INVALID`` -- the rules text could not be read well enough to trade on.
      Terminal, and the honest verdict for the ~half of markets that parse as AMBIGUOUS
      or UNPARSEABLE.
    * ``MONITORED`` -- classified into a tradeable category *and* its resolution text
      accepted. This is the only state from which anything may be traded.
    * ``CLASSIFIED`` -- read, and not tradeable by category. Recorded rather than
      rejected, because "we know what this is and have no model for it" is a different
      fact from "we could not read it".
    """
    if not resolution.is_tradeable:
        return MarketState.MARKET_INVALID
    if classification.is_tradeable:
        return MarketState.MONITORED
    return MarketState.CLASSIFIED
