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
    which is the point. "circuit breakers" was removed on 2026-09-13 when the
    supervisor was wired into the health loop; reconciliation stays, qualified with
    what it is waiting on.
    """
    declared = " | ".join(NOT_YET_WIRED)
    assert "signal loop" in declared
    assert "reconciliation" in declared
    assert "position manager" in declared
    # Wired as of Phase 5 item 24: the health loop feeds BreakerSupervisor.
    assert "circuit breakers" not in declared


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
