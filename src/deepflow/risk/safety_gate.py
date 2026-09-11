"""Safety gate. Section 18.

Every trade passes the same ordered checklist. Any mandatory failure is a hard
NO TRADE -- there is no weighting, no score, no "two out of three".

The structure matters as much as the checks. Making each check a named object
with a mandatory flag means the result is a readable record of exactly which
condition blocked a trade, which is what the journal and the dashboard need,
and it makes adding a check impossible to do without deciding whether it is
mandatory.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class CheckId(StrEnum):
    """The mandatory checklist, in evaluation order."""

    MARKET_VALID = "MARKET_VALID"
    RESOLUTION_VALID = "RESOLUTION_VALID"
    CLASSIFICATION_VALID = "CLASSIFICATION_VALID"
    DATA_FRESH = "DATA_FRESH"
    MODEL_AVAILABLE = "MODEL_AVAILABLE"
    PROBABILITY_VALID = "PROBABILITY_VALID"
    POSITIVE_NET_EV = "POSITIVE_NET_EV"
    LIQUIDITY_SUFFICIENT = "LIQUIDITY_SUFFICIENT"
    SPREAD_ACCEPTABLE = "SPREAD_ACCEPTABLE"
    SLIPPAGE_ACCEPTABLE = "SLIPPAGE_ACCEPTABLE"
    CAPITAL_AVAILABLE = "CAPITAL_AVAILABLE"
    EXPOSURE_ACCEPTABLE = "EXPOSURE_ACCEPTABLE"
    NO_DUPLICATE_ORDER = "NO_DUPLICATE_ORDER"
    EXECUTION_HEALTHY = "EXECUTION_HEALTHY"
    RISK_APPROVED = "RISK_APPROVED"


@dataclass(frozen=True, slots=True)
class CheckResult:
    check: CheckId
    passed: bool
    detail: str = ""
    mandatory: bool = True


@dataclass(frozen=True, slots=True)
class GateDecision:
    """Verdict plus the full evidence trail."""

    approved: bool
    results: tuple[CheckResult, ...]

    @property
    def failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.results if not r.passed)

    @property
    def blocking_failures(self) -> tuple[CheckResult, ...]:
        return tuple(r for r in self.failures if r.mandatory)

    @property
    def reason(self) -> str:
        """One-line explanation, suitable for the journal and the dashboard."""
        if self.approved:
            return "all checks passed"
        return "; ".join(f"{r.check}: {r.detail or 'failed'}" for r in self.blocking_failures)


#: A check is a callable over the evaluation context returning a CheckResult.
CheckFn = Callable[["GateContext"], CheckResult]


@dataclass(slots=True)
class GateContext:
    """Everything the checks read.

    A plain container rather than a pile of parameters, so adding a check never
    changes the gate's signature.

    TODO(skeleton): populate with market, classification, resolution, snapshot,
    estimate, EV assessment, sizing result, exposure state and execution health.
    """

    payload: dict[str, object] = field(default_factory=dict)


class SafetyGate:
    """Runs the checklist.

    Evaluates every check rather than short-circuiting on the first failure.
    Knowing a trade failed on four conditions rather than one is what tells you
    whether a gate is mis-tuned or the opportunity was simply bad.
    """

    def __init__(self, checks: Sequence[tuple[CheckId, CheckFn]] | None = None) -> None:
        self._checks: list[tuple[CheckId, CheckFn]] = list(checks or [])

    def register(self, check_id: CheckId, fn: CheckFn) -> None:
        self._checks.append((check_id, fn))

    def evaluate(self, context: GateContext) -> GateDecision:
        """Run all registered checks.

        A check that raises is treated as a *failure*, never as a pass. An
        exception in a safety check is the case where defaulting to permissive
        would be most expensive.
        """
        results: list[CheckResult] = []
        for check_id, fn in self._checks:
            try:
                results.append(fn(context))
            except Exception as exc:
                results.append(
                    CheckResult(
                        check=check_id,
                        passed=False,
                        detail=f"check raised: {exc!r}",
                        mandatory=True,
                    )
                )

        missing = set(CheckId) - {r.check for r in results}
        for check_id in missing:
            results.append(
                CheckResult(
                    check=check_id,
                    passed=False,
                    detail="check not registered",
                    mandatory=True,
                )
            )

        decision_results = tuple(results)
        approved = not any(r.mandatory and not r.passed for r in decision_results)
        return GateDecision(approved=approved, results=decision_results)
