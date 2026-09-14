"""Orchestrator lifecycle and the guards on it.

Composition is not tested here -- that needs a venue and a database, and
``scripts/verify_phase1.py`` covers it live. What is tested is the behaviour that
must hold regardless of what is wired: the LIVE-mode refusal, idempotent start,
safe shutdown, and that a dead task is surfaced rather than swallowed.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest

from deepflow.config.settings import LIVE_ACK_PHRASE, Settings
from deepflow.core.enums import RunMode
from deepflow.pipeline.orchestrator import NOT_YET_WIRED, Orchestrator


def _live_settings() -> Settings:
    """A LIVE configuration that passes every settings-level guard.

    Built deliberately: the point is to prove the *orchestrator* refuses LIVE even
    when the configuration is otherwise complete. A Settings object that failed
    validation would prove only that validation works.
    """
    return Settings(
        mode=RunMode.LIVE,
        live_trading_confirmed=True,
        live_trading_ack=LIVE_ACK_PHRASE,
        polymarket={  # type: ignore[arg-type]
            "private_key": "0x" + "11" * 32,
            "wallet_address": "0x" + "22" * 20,
            "relayer_api_key": "relayer-key",
        },
        api={"jwt_secret": "s" * 48},  # type: ignore[arg-type]
    )


async def test_live_mode_is_refused_while_no_model_exists_and_writes_are_unverified() -> None:
    """The interlock that matters most right now.

    Every settings-level guard can be satisfied and the process still must not start in
    LIVE. As of Phase 5 the reason is no longer missing plumbing — the execution
    adapter, the reconciler and the breakers all exist. It is that no probability model
    exists, so the decision layer runs on injected estimates, and that no order has
    ever been submitted to this venue, so every write path is unverified against it.
    """
    orchestrator = Orchestrator(settings=_live_settings())
    with pytest.raises(RuntimeError, match="refusing to start in LIVE mode") as caught:
        await orchestrator.start()
    assert not orchestrator.is_running
    # Pinned because a refusal that states a reason which is no longer true is worse
    # than a bare refusal: it sends the next reader to fix the wrong thing.
    message = str(caught.value)
    assert "No probability model exists" in message
    assert "No order has ever been submitted" in message


async def test_live_refusal_happens_before_any_connection() -> None:
    """The check must precede construction of the venue session, or a refused LIVE
    start still opens a socket with a signing key loaded."""
    orchestrator = Orchestrator(settings=_live_settings())
    with pytest.raises(RuntimeError):
        await orchestrator.start()
    assert orchestrator._venue is None
    assert orchestrator._engine is None


async def test_stop_is_safe_before_start() -> None:
    """Shutdown runs on paths where startup failed, so it cannot assume a
    successful start -- otherwise a failed boot masks its own cause with an
    AttributeError from the teardown."""
    await Orchestrator(settings=Settings()).stop()


async def test_is_running_is_false_before_start() -> None:
    assert not Orchestrator(settings=Settings()).is_running


async def test_unwired_tasks_are_declared() -> None:
    """Logged at startup on purpose: "the bot is running" must not be mistaken for
    "the bot is trading".

    Matched on substrings rather than exact strings, so an entry can gain detail
    without breaking the test — but an entry *leaving* this list still does break it,
    which is the point. Each removal is dated and reasoned here rather than quietly
    deleted, because this list is what a reader trusts when asking what the process
    actually does.

    Removed: "circuit breakers" on 2026-09-13, when the health loop began feeding
    BreakerSupervisor. "signal loop" and "crypto price stream" on 2026-09-14, when the
    reference feed and the decision chain were wired — the process now prices short-dated
    crypto markets, runs them through EV, the gate and risk, and journals every verdict.

    What must stay: **execution**, because a journalled approval is still not an order,
    and no execution adapter is constructed in this process at all.
    """
    declared = " | ".join(NOT_YET_WIRED)
    assert "execution" in declared
    assert "reconciliation" in declared
    assert "position manager" in declared
    # Wired as of Phase 5 item 24: the health loop feeds BreakerSupervisor.
    assert "circuit breakers" not in declared
    # Wired 2026-09-14: the reference feed holds the TWAP series and the stream loop
    # runs the decision chain. A stale entry here would be worse than no list.
    assert "signal loop" not in declared
    assert "crypto price stream" not in declared


async def test_a_dead_task_is_surfaced() -> None:
    """A long-lived task ending on its own is never normal.

    The default behaviour -- an exception sitting unretrieved on a discarded task --
    is how a system keeps reporting itself healthy with its data feed dead.
    """
    orchestrator = Orchestrator(settings=Settings())
    logged: list[tuple[str, dict[str, object]]] = []

    async def boom() -> None:
        raise RuntimeError("feed died")

    import deepflow.pipeline.orchestrator as module

    class _Recorder:
        def error(self, event: str, **kw: object) -> None:
            logged.append((event, kw))

        def __getattr__(self, _name: str) -> object:
            return lambda *a, **k: None

    original = module.log
    module.log = _Recorder()  # type: ignore[assignment]
    try:
        orchestrator._spawn(boom(), name="market-stream")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    finally:
        module.log = original

    assert any(event == "orchestrator.task_failed" for event, _ in logged)


async def test_cancelled_tasks_are_not_reported_as_failures() -> None:
    """Shutdown cancels everything. Reporting those as failures would bury a real
    one in the noise of every clean stop."""
    orchestrator = Orchestrator(settings=Settings())
    logged: list[str] = []

    async def forever() -> None:
        await asyncio.sleep(3600)

    import deepflow.pipeline.orchestrator as module

    class _Recorder:
        def error(self, event: str, **kw: object) -> None:
            logged.append(event)

        def __getattr__(self, _name: str) -> object:
            return lambda *a, **k: None

    original = module.log
    module.log = _Recorder()  # type: ignore[assignment]
    try:
        orchestrator._spawn(forever(), name="health")
        await orchestrator.stop()
    finally:
        module.log = original

    assert logged == []
    assert not orchestrator.is_running


async def test_the_reference_feed_subscribes_to_every_symbol_unfiltered() -> None:
    """An empty filter, on purpose — and this is a correction, not a shortcut.

    A named list of eight assets was wrong about three of them (ada, link and avax are not
    published; bnb, hype and zec are, and the venue runs the cadence on them) — §79. An
    unfiltered subscription receives exactly what the venue publishes, so an asset added
    tomorrow needs no release, and no asset is silently abstained on.

    The whole set rather than what discovery found, because an up/down market's strike is
    the TWAP at the instant its window opens and the venue publishes it nowhere (§77): a
    feed subscribed when a market is *noticed* has already missed it permanently.
    """
    from deepflow.pipeline.orchestrator import REFERENCE_SYMBOLS

    assert REFERENCE_SYMBOLS == ()


async def test_a_symbol_is_derived_from_the_slug_without_an_allowlist() -> None:
    """The assets the venue actually runs, measured live, all resolve — including the three
    a hardcoded list had missed."""
    from deepflow.engines.crypto.btc_5m import _chainlink_symbol

    for asset in ("btc", "eth", "sol", "xrp", "bnb", "hype", "doge", "zec"):
        assert _chainlink_symbol(f"{asset}-updown-5m-1789303800") == f"{asset}/usd"


async def test_the_btc_engine_is_reachable_from_the_registry() -> None:
    """The wiring check no per-module test can make.

    `games.py` and `engines/sports/rules/` were each fully tested and reachable from
    nothing, and Phases 4-6 were in the same state until this was wired. Building the
    registry the way the orchestrator does is what proves an estimate has a route to the
    decision layer.
    """
    from deepflow.config.thresholds import Btc5mThresholds
    from deepflow.core.enums import MarketCategory
    from deepflow.engines.crypto.btc_5m import Btc5mEngine
    from deepflow.engines.crypto.reference import TwapReference
    from deepflow.engines.registry import EngineRegistry

    registry = EngineRegistry()
    registry.register(Btc5mEngine(Btc5mThresholds(), reference=TwapReference()))
    assert MarketCategory.BTC_5M in registry.registered_categories


async def test_the_decision_chain_is_constructed_as_a_whole() -> None:
    """A partial chain is worse than none: an estimate with no gate behind it is a number
    nobody vetoed, and it would still reach the journal looking decided."""
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    source = inspect.getsource(Orc.start)
    for component in ("EngineRegistry()", "default_gate()", "RiskEngine(", "JournalRecorder("):
        assert component in source, f"{component} must be constructed in start()"


async def test_every_category_with_an_engine_has_execution_limits() -> None:
    """The gate cannot examine a trade it has no limits for, and the limits are not
    transferable: politics books measured a median spread of 39 bps against crypto's 217, and
    a tick of 0.001 against 0.01. A category with an engine and no limits fails closed, which
    is safe and also silent — so this pins the pairing instead.
    """
    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    orchestrator = Orc(settings=Settings(_env_file=None))
    from deepflow.config.thresholds import Btc5mThresholds
    from deepflow.engines.crypto.btc_5m import Btc5mEngine
    from deepflow.engines.crypto.reference import TwapReference
    from deepflow.engines.geopolitics.engine import GeopoliticalEngine
    from deepflow.engines.geopolitics.events import EventPipeline
    from deepflow.engines.politics.political import PoliticalEngine
    from deepflow.engines.registry import EngineRegistry

    registry = EngineRegistry()
    registry.register(Btc5mEngine(Btc5mThresholds(), reference=TwapReference()))
    registry.register(PoliticalEngine(EventPipeline()))
    registry.register(
        GeopoliticalEngine(EventPipeline(), Settings(_env_file=None).thresholds.geopolitics)
    )

    for category in registry.registered_categories:
        assert orchestrator._limits_for(category) is not None, category


async def test_the_political_and_geopolitical_engines_are_registered() -> None:
    """They are 100 of the 100 markets a general sweep returns — 98 politics and 2
    geopolitics — and while no engine claimed them every one was dropped from the decision
    context. Built, tested, and reachable from nothing.
    """
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    source = inspect.getsource(Orc.start)
    assert "PoliticalEngine(" in source
    assert "GeopoliticalEngine(" in source
    # One pipeline between them: corroboration is counted across a window of claims, and two
    # pipelines would each see half the reports and neither reach the two-publisher bar.
    assert source.count("EventPipeline(") == 1


async def test_a_prior_naming_an_untracked_market_is_reported() -> None:
    """A typo in a slug otherwise looks exactly like a market that has closed."""
    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    orchestrator = Orc(
        settings=Settings(
            _env_file=None,
            base_rates={"no-such-market": {"probability": "0.3", "source": "poll average"}},
        )
    )
    orchestrator._apply_base_rates()  # no engines built yet, so this must not raise
    assert orchestrator._priors_applied == 0


def test_a_prior_must_name_its_source() -> None:
    """An unsourced prior is a guess wearing a probability's clothes, and these markets have
    no other source of a number — the price being circular and forbidden."""
    import pytest as _pytest

    from deepflow.config.settings import BaseRateConfig

    with _pytest.raises(ValueError):
        BaseRateConfig(probability=Decimal("0.3"), source="")


async def test_the_settlement_loop_is_wired() -> None:
    """Calibration's raw material, and the reason ``_calibrate`` was an identity
    function for so long.

    Predictions were never the missing half -- ``signals`` stored a model probability
    all along. What nothing recorded was how a market *resolved*, so no amount of
    running could have produced a fit. The loop that records it has to be reachable
    from ``start()`` or the tables stay empty and the fitter keeps declining for want
    of samples, which looks exactly like "not enough data yet".
    """
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    source = inspect.getsource(Orc.start)
    assert "SettlementRecorder(" in source
    assert 'name="settlement"' in source
    # The curve an operator activated must be installed before any estimate is made,
    # or the first minutes of a run are silently uncalibrated.
    assert "_install_calibrators()" in source


async def test_every_estimate_is_recorded_not_only_the_traded_ones() -> None:
    """Calibrating on the traded subset would fit the curve to the region where this
    system already believed it had an edge -- and leave it blind everywhere else,
    which is the part a fit exists to correct.

    So the recording call sits in ``_consider``, beside the estimate counter, rather
    than anywhere downstream of the gate.
    """
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    source = inspect.getsource(Orc._consider)
    assert "_record_prediction(" in source
    recorded = source.index("_record_prediction(")
    decided = source.index("_decide(")
    assert recorded < decided, "predictions must be recorded before the gate can refuse"


async def test_predictions_are_sampled_rather_than_stored_per_snapshot() -> None:
    """An engine re-estimates on every book update, and a 5-minute crypto window
    sampled that way yields hundreds of rows settled by one coin flip. Stored whole,
    they make a curve fitted on a handful of outcomes look like one fitted on
    thousands -- with the false confidence densest in the 0.85-0.98 band.
    """
    from deepflow.pipeline.orchestrator import PREDICTION_SAMPLE_SECONDS

    assert PREDICTION_SAMPLE_SECONDS >= 60.0


async def test_the_sampling_interval_admits_several_horizons_per_short_market() -> None:
    """The other side of that trade-off: sampling too coarsely would record one row
    per 5-minute market and lose the horizon variation entirely. A model four minutes
    out and one minute out are different estimators."""
    from deepflow.engines.crypto.btc_5m import Btc5mEngine  # noqa: F401
    from deepflow.pipeline.orchestrator import PREDICTION_SAMPLE_SECONDS

    assert 300 / PREDICTION_SAMPLE_SECONDS >= 4


async def test_a_prediction_failure_never_costs_the_decision() -> None:
    """A prediction row is evidence for a future fit, not a safety mechanism. Losing
    one must not stop the estimate it came from being acted on."""
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    source = inspect.getsource(Orc._record_prediction)
    assert "_prediction_failures" in source
    assert "except Exception" in source


async def test_the_football_engine_is_registered_and_fed() -> None:
    """Built, tested and reachable from nothing is the failure this repo keeps
    repeating -- `games.py`, the sports rules, and both event-driven engines. A model
    that no market can reach is not a model."""
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    source = inspect.getsource(Orc.start)
    assert "FootballEngine(" in source
    assert "MatchStateStore(" in source

    # The store is useless unless the fixture sweep fills it.
    sweep = inspect.getsource(Orc._sweep_live_games)
    assert "_observe_fixture(" in sweep


async def test_fixture_state_is_collected_per_sweep_not_per_snapshot() -> None:
    """§81: classification and resolution run inline in the stream consumer dropped
    1.3 million events in ten minutes while reporting itself healthy. Parsing a
    fixture per book update is the same mistake with a different input."""
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    assert "_observe_fixture(" not in inspect.getsource(Orc._consider)
    assert "_observe_fixture(" in inspect.getsource(Orc._sweep_live_games)


async def test_football_has_execution_limits() -> None:
    """A category with an engine and no limits fails closed, which is safe and
    completely silent (§87)."""
    from deepflow.core.enums import MarketCategory
    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    orchestrator = Orc(settings=Settings(_env_file=None))
    assert orchestrator._limits_for(MarketCategory.FOOTBALL) is not None


async def test_a_fixture_with_no_named_sides_is_not_stored() -> None:
    """A soccer result market is a three-way group whose members are told apart only
    by matching a team name, so state without names would let the engine get further
    before abstaining and tell nobody why."""
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    source = inspect.getsource(Orc._observe_fixture)
    assert "home_team" in source and "fixture_unnamed" in source


async def test_snapshots_are_never_written_inline_with_the_fold() -> None:
    """**§91.** A session round trip per book update made the stream consumer slower
    than the pump, and the cost was not merely lost history: every queue overflow marks
    *every* book gapped, so the feed degraded its own data and the entry gate then
    refused all of it -- 97% of 2.7 million rows came out DEGRADED.

    Measured over ~90 seconds on 194 tracked markets: 529,756 events dropped with the
    inline write, zero without it.
    """
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    loop = inspect.getsource(Orc._stream_loop)
    assert "_buffer_snapshot(" in loop
    assert "await self._persist(" not in loop
    assert not hasattr(Orc, "_persist"), "the inline persist path should be gone, not kept"


async def test_the_snapshot_writer_is_a_task_of_its_own() -> None:
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    assert 'name="snapshot-writer"' in inspect.getsource(Orc.start)


async def test_a_flush_is_bounded_work() -> None:
    """An unbounded flush leaves a bigger buffer for the next one, which is slower
    again. Observed before the cap: the writer finished one batch of 4,056 rows and
    never completed another while the buffer grew past 44,000."""
    from deepflow.pipeline.orchestrator import SNAPSHOT_MAX_BATCH_ROWS

    assert 0 < SNAPSHOT_MAX_BATCH_ROWS <= 10_000


async def test_snapshots_are_sampled_per_market() -> None:
    """A book updates tens of times a second; entries run against a 3-10 second age
    budget. Everything finer is resolution nobody reads, at a write volume that broke
    the feed."""
    from deepflow.pipeline.orchestrator import SNAPSHOT_SAMPLE_SECONDS

    assert SNAPSHOT_SAMPLE_SECONDS >= 1.0


async def test_the_buffer_is_drained_on_shutdown() -> None:
    """Otherwise every clean stop silently discards up to a flush interval of history,
    which shows up only as a gap in a backtest months later."""
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    assert "_flush_snapshots()" in inspect.getsource(Orc.stop)


async def test_a_silent_eviction_is_counted() -> None:
    """A deque at maxlen drops the oldest without saying so. The queue overflow this
    replaced went unnoticed until it had cost 2.7 million degraded rows, so the
    replacement reports its own losses."""
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    assert "_snapshots_evicted" in inspect.getsource(Orc._buffer_snapshot)
    assert "snapshots_evicted=" in inspect.getsource(Orc._health_loop)


async def test_the_process_records_what_it_decided_about_each_market() -> None:
    """Without this the ``markets`` table holds only the venue's own fields.

    Measured before the fix: **398 rows at DISCOVERED / NOT_CHECKED / UNKNOWN**, written
    by the running process, sitting beside 161 properly classified rows that a
    verification script had written once by hand. The table recorded what the venue said
    and nothing about what this system thought.
    """
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    assert "_persist_verdicts()" in inspect.getsource(Orc._sweep_once)
    source = inspect.getsource(Orc._persist_verdicts)
    assert "record_classification(" in source


async def test_a_verdict_is_kept_for_every_market_not_only_priceable_ones() -> None:
    """The decision context keeps what an engine can price; the database should record
    what we concluded about everything, which is a much larger set."""
    import inspect

    from deepflow.pipeline.orchestrator import Orchestrator as Orc

    source = inspect.getsource(Orc._rebuild_decision_context)
    verdict_line = source.index("verdicts[market.condition_id]")
    skip_line = source.index("if self._engines.resolve(classification) is None")
    assert verdict_line < skip_line, "the verdict must be recorded before the filter"


def test_lifecycle_distinguishes_unreadable_from_unmodelled() -> None:
    """ "We could not read this market" and "we read it and have no model" are
    different facts, and collapsing them loses the one that says where to spend
    effort."""
    from deepflow.core.domain import Classification, ResolutionCriteria
    from deepflow.core.enums import MarketCategory, ResolutionValidity
    from deepflow.core.state_machine import MarketState
    from deepflow.pipeline.orchestrator import _lifecycle_for

    tradeable = Classification(
        category=MarketCategory.FOOTBALL, confidence=Decimal("0.9"), rationale="tag"
    )
    unknown = Classification(
        category=MarketCategory.UNKNOWN, confidence=Decimal(0), rationale="no match"
    )
    valid = ResolutionCriteria(validity=ResolutionValidity.VALID)
    ambiguous = ResolutionCriteria(validity=ResolutionValidity.AMBIGUOUS)

    assert _lifecycle_for(tradeable, valid) is MarketState.MONITORED
    assert _lifecycle_for(unknown, valid) is MarketState.CLASSIFIED
    assert _lifecycle_for(tradeable, ambiguous) is MarketState.MARKET_INVALID
