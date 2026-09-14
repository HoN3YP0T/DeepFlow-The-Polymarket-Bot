"""Orchestrator lifecycle and the guards on it.

Composition is not tested here -- that needs a venue and a database, and
``scripts/verify_phase1.py`` covers it live. What is tested is the behaviour that
must hold regardless of what is wired: the LIVE-mode refusal, idempotent start,
safe shutdown, and that a dead task is surfaced rather than swallowed.
"""

from __future__ import annotations

import asyncio

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
