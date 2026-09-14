"""Safety gate tests. Section 18: any mandatory failure is a hard NO TRADE."""

from __future__ import annotations

from deepflow.risk.safety_gate import (
    CheckId,
    CheckResult,
    GateContext,
    SafetyGate,
)


def _passing(check: CheckId):
    return lambda _ctx: CheckResult(check=check, passed=True)


def _failing(check: CheckId, detail: str = "nope"):
    return lambda _ctx: CheckResult(check=check, passed=False, detail=detail)


def _gate_with_all_passing() -> SafetyGate:
    gate = SafetyGate()
    for check in CheckId:
        gate.register(check, _passing(check))
    return gate


def test_all_checks_passing_approves() -> None:
    decision = _gate_with_all_passing().evaluate(GateContext())
    assert decision.approved
    assert decision.failures == ()


def test_single_mandatory_failure_blocks() -> None:
    gate = SafetyGate()
    for check in CheckId:
        if check is CheckId.POSITIVE_NET_EV:
            gate.register(check, _failing(check, "net EV -0.004"))
        else:
            gate.register(check, _passing(check))

    decision = gate.evaluate(GateContext())
    assert not decision.approved
    assert "POSITIVE_NET_EV" in decision.reason
    assert "net EV -0.004" in decision.reason


def test_unregistered_check_blocks() -> None:
    """A missing check is a failure, never an implicit pass. Forgetting to wire
    one in must not silently disable it."""
    gate = SafetyGate()
    gate.register(CheckId.MARKET_VALID, _passing(CheckId.MARKET_VALID))

    decision = gate.evaluate(GateContext())
    assert not decision.approved
    assert any("not registered" in r.detail for r in decision.blocking_failures)


def test_raising_check_blocks_rather_than_propagating() -> None:
    """An exception inside a safety check is exactly where defaulting to
    permissive would be most expensive."""

    def explode(_ctx: GateContext) -> CheckResult:
        raise RuntimeError("model unavailable")

    gate = SafetyGate()
    for check in CheckId:
        gate.register(check, explode if check is CheckId.MODEL_AVAILABLE else _passing(check))

    decision = gate.evaluate(GateContext())
    assert not decision.approved
    assert any("check raised" in r.detail for r in decision.blocking_failures)


def test_all_checks_run_even_after_a_failure() -> None:
    """No short-circuit: knowing a trade failed four conditions rather than one
    is what distinguishes a mis-tuned gate from a bad opportunity."""
    gate = SafetyGate()
    for check in CheckId:
        gate.register(check, _failing(check))

    decision = gate.evaluate(GateContext())
    assert len(decision.blocking_failures) == len(CheckId)


def test_checklist_covers_the_specified_conditions() -> None:
    """Guards against a check being quietly dropped from -- or added to -- the enum.

    Adding a check is a deliberate act that should require editing this list. The
    two beyond the original fifteen were added on 2026-09-13 from findings 63-64,
    which described hazards the original set could not express: the venue clearing
    the book at a contest start, and a model priced off a feed the market does not
    settle against.
    """
    required = {
        "MARKET_VALID",
        "RESOLUTION_VALID",
        "CLASSIFICATION_VALID",
        "DATA_FRESH",
        "MODEL_AVAILABLE",
        "PROBABILITY_VALID",
        "POSITIVE_NET_EV",
        "LIQUIDITY_SUFFICIENT",
        "SPREAD_ACCEPTABLE",
        "SLIPPAGE_ACCEPTABLE",
        "CAPITAL_AVAILABLE",
        "EXPOSURE_ACCEPTABLE",
        "NO_DUPLICATE_ORDER",
        "EXECUTION_HEALTHY",
        "RISK_APPROVED",
        "BOOK_CLEARED_AT_START",
        "REFERENCE_FEED_MATCHED",
        "PROBABILITY_IN_BAND",
    }
    assert {c.value for c in CheckId} == required
