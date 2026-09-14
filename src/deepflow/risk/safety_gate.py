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
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from deepflow.core.domain import (
    Classification,
    EvAssessment,
    Market,
    MarketSnapshot,
    ProbabilityEstimate,
    ResolutionCriteria,
)
from deepflow.core.enums import DataQuality, MarketCategory, ResolutionValidity
from deepflow.risk.limits import RiskVerdict


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

    #: Added 2026-09-13 from findings 63-64, which described hazards the original
    #: fifteen could not express.
    BOOK_CLEARED_AT_START = "BOOK_CLEARED_AT_START"
    """The venue empties the book when a contest starts, best-effort. An early start
    can leave a resting order live into a game in progress -- the one state a
    pre-contest price was never meant to survive."""
    REFERENCE_FEED_MATCHED = "REFERENCE_FEED_MATCHED"
    """The price source we modelled from must be the source the market settles
    against. Crypto up/down settles on a Chainlink TWAP with a 30-second lookback,
    not spot, and at a five-minute horizon that basis is the whole edge."""


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

    Every field defaults to ``None``, and **``None`` fails the check that reads
    it**. That is the whole design: a context assembled by a caller that forgot a
    field produces a refusal naming the missing input, never an approval. A gate
    whose checks pass because nothing was supplied is worse than no gate, because
    the journal records that the checklist ran.

    The checks are pure functions over this container and never call an engine.
    Producers (risk, exposure, execution health) hand in their verdicts, which keeps
    the gate free of the ordering and failure semantics of the things it gates.
    """

    now: datetime | None = None

    # --- What the market is ---
    market: Market | None = None
    classification: Classification | None = None
    resolution: ResolutionCriteria | None = None

    # --- What we believe ---
    snapshot: MarketSnapshot | None = None
    estimate: ProbabilityEstimate | None = None
    ev: EvAssessment | None = None

    # --- What we are allowed to do ---
    risk: RiskVerdict | None = None
    exposure_breach: str | None = None
    """The name of a limit a prospective stake would breach. ``None`` means no
    breach -- so this one field is the exception: absent is the *passing* state."""
    capital_available_usdc: Decimal | None = None
    stake_usdc: Decimal | None = None

    # --- State of the world ---
    duplicate_order_exists: bool | None = None
    execution_healthy: bool | None = None

    # --- Limits, from the strategy rather than assumed here ---
    max_data_age_seconds: float | None = None
    max_spread_bps: Decimal | None = None
    max_slippage_bps: Decimal | None = None
    min_liquidity_usdc: Decimal | None = None
    min_confidence: int | None = None

    # --- Findings 63-64 ---
    contest_start: datetime | None = None
    """When the venue will clear the book. ``eventStartTime`` for a crypto window,
    ``gameStartTime`` for a fixture."""
    no_entry_seconds_before_start: float | None = None
    model_reference_feed: str | None = None
    """The price source the probability was derived from, e.g. ``chainlink_twap_30s``."""
    settlement_reference_feed: str | None = None
    """The source the market settles against, read from its resolution text."""

    payload: dict[str, object] = field(default_factory=dict)
    """Free-form extras for a check that has not earned a field yet."""


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


# ===========================================================================
# The checks
# ===========================================================================
# Each is a pure function of the context. Two conventions hold throughout, and
# both exist because the alternative fails open:
#
# * A missing input **fails**, with a detail naming what was missing. A check
#   cannot pass on the basis of not having looked.
# * The detail string is written for a human reading a rejection in the journal
#   six weeks later, so it carries the numbers, not just the verdict.


def _fail(check: CheckId, detail: str) -> CheckResult:
    return CheckResult(check=check, passed=False, detail=detail)


def _pass(check: CheckId, detail: str = "") -> CheckResult:
    return CheckResult(check=check, passed=True, detail=detail)


def check_market_valid(ctx: GateContext) -> CheckResult:
    """Open, accepting orders, with a live book -- and not settling on a delay.

    ``seconds_delay > 0`` is included here rather than left to configuration: such
    a market accepts an order and does not match it, so every entry outlives its
    timeout and every fill arrives as a surprise. It is a property of the market,
    which is what this check is about.
    """
    market = ctx.market
    if market is None:
        return _fail(CheckId.MARKET_VALID, "no market supplied")
    # Spelled out rather than delegated to a helper. This is the check a reader
    # goes to when asking "why did it trade a closed market", and every condition
    # being visible here is worth more than the brevity of a shared predicate.
    if not (
        market.active and not market.closed and market.accepting_orders and market.enable_order_book
    ):
        return _fail(
            CheckId.MARKET_VALID,
            f"not tradeable: active={market.active} closed={market.closed} "
            f"accepting_orders={market.accepting_orders} book={market.enable_order_book}",
        )
    if market.seconds_delay > 0:
        return _fail(
            CheckId.MARKET_VALID,
            f"delayed matching: seconds_delay={market.seconds_delay}",
        )
    return _pass(CheckId.MARKET_VALID)


def check_resolution_valid(ctx: GateContext) -> CheckResult:
    """Only ``VALID`` resolution text is tradeable.

    ``AMBIGUOUS`` is the common rejection and the one worth naming explicitly:
    roughly a third of markets land there, and they are not *unreadable* -- they
    are readable and leave a judgement call to someone else.
    """
    resolution = ctx.resolution
    if resolution is None:
        return _fail(CheckId.RESOLUTION_VALID, "resolution not validated")
    if resolution.validity is not ResolutionValidity.VALID:
        return _fail(CheckId.RESOLUTION_VALID, f"validity={resolution.validity.value}")
    return _pass(CheckId.RESOLUTION_VALID)


def check_classification_valid(ctx: GateContext) -> CheckResult:
    """A category we have a model for, held with enough confidence to act on."""
    classification = ctx.classification
    if classification is None:
        return _fail(CheckId.CLASSIFICATION_VALID, "not classified")
    if not classification.is_tradeable:
        return _fail(CheckId.CLASSIFICATION_VALID, "category UNKNOWN")
    if classification.category is MarketCategory.OTHER_SPORTS:
        return _fail(
            CheckId.CLASSIFICATION_VALID,
            "OTHER_SPORTS: recognised as sport, no model for the league",
        )
    return _pass(CheckId.CLASSIFICATION_VALID, classification.category.value)


def check_data_fresh(ctx: GateContext) -> CheckResult:
    """Freshness and consistency, in that order of severity.

    ``INCONSISTENT`` is separated from merely old because no amount of waiting
    fixes it: it means our folded book disagrees with the venue's own reported
    touch, so the book must be re-anchored rather than aged out.

    **``DEGRADED`` is refused here, and it was not until 2026-09-14.** The rule that
    only ``FRESH`` data may open new risk was written in two places --
    :attr:`MarketSnapshot.entries_allowed` and ``FeatureEngine.assess_snapshot``'s
    docstring -- and enforced in neither: ``entries_allowed`` had no callers anywhere in
    the codebase, and this check tested for ``INCONSISTENT`` and ``STALE`` and let
    ``DEGRADED`` through. A gate that passes because the check was never written is the
    same failure as one that passes for want of an input, and harder to notice.

    What ``DEGRADED`` means here is a **gap**: the stream marked the book after a
    reconnect or after dropping an update, so the folded state may be missing a level
    change that already happened. That is not a stale price, it is a possibly-wrong one,
    and the difference matters most in exactly the 0.85-0.98 band this system trades,
    where one missed level is most of the edge.

    This gate governs **entries**. Managing an existing position on degraded data is
    still the right call -- refusing to look at an open trade is worse than looking at
    it through an imperfect book -- and the exit path does not run through here.
    """
    snapshot = ctx.snapshot
    if snapshot is None:
        return _fail(CheckId.DATA_FRESH, "no snapshot")
    if snapshot.quality is DataQuality.INCONSISTENT:
        return _fail(CheckId.DATA_FRESH, "book inconsistent: folded state disagrees with venue")
    if snapshot.quality is DataQuality.STALE:
        return _fail(CheckId.DATA_FRESH, "quality=STALE")
    if not snapshot.entries_allowed:
        # Reads the domain's own rule rather than restating it, so the invariant has
        # exactly one definition and this check cannot drift away from it.
        return _fail(
            CheckId.DATA_FRESH,
            f"quality={snapshot.quality.value}: book has a gap, entries need FRESH",
        )

    if ctx.now is not None and ctx.max_data_age_seconds is not None:
        age = (ctx.now - snapshot.captured_at).total_seconds()
        if age > ctx.max_data_age_seconds:
            return _fail(
                CheckId.DATA_FRESH,
                f"age {age:.1f}s over budget {ctx.max_data_age_seconds:.1f}s",
            )
        return _pass(CheckId.DATA_FRESH, f"age {age:.1f}s, quality={snapshot.quality.value}")
    return _fail(CheckId.DATA_FRESH, "no clock or age budget supplied")


def check_model_available(ctx: GateContext) -> CheckResult:
    """A probability came from somewhere nameable."""
    estimate = ctx.estimate
    if estimate is None:
        return _fail(CheckId.MODEL_AVAILABLE, "no probability estimate")
    if not estimate.engine:
        return _fail(CheckId.MODEL_AVAILABLE, "estimate has no engine name")
    return _pass(CheckId.MODEL_AVAILABLE, estimate.engine)


def check_probability_valid(ctx: GateContext) -> CheckResult:
    """A probability at the boundary is a refusal wearing a number's clothes.

    0 and 1 are rejected rather than clamped. A model returning certainty about a
    future event has failed, and the useful response is to say so here rather than
    to size a position off it.
    """
    estimate = ctx.estimate
    if estimate is None:
        return _fail(CheckId.PROBABILITY_VALID, "no probability estimate")
    probability = estimate.calibrated_probability
    if probability <= 0 or probability >= 1:
        return _fail(CheckId.PROBABILITY_VALID, f"degenerate probability {probability}")
    if (
        ctx.min_confidence is not None
        and ctx.ev is not None
        and ctx.ev.confidence < ctx.min_confidence
    ):
        return _fail(
            CheckId.PROBABILITY_VALID,
            f"confidence {ctx.ev.confidence} below {ctx.min_confidence}",
        )
    return _pass(CheckId.PROBABILITY_VALID, f"p={probability}")


def check_positive_net_ev(ctx: GateContext) -> CheckResult:
    """The one check the whole system exists to fail.

    Net EV, never edge. A positive edge that costs more than it is worth is not a
    trade, and this is the last place that distinction can be enforced cheaply.
    """
    ev = ctx.ev
    if ev is None:
        return _fail(CheckId.POSITIVE_NET_EV, "no EV assessment")
    if not ev.is_positive:
        return _fail(
            CheckId.POSITIVE_NET_EV,
            f"net_ev={ev.net_ev} (edge={ev.edge}, costs={ev.costs.total_bps}bps)",
        )
    return _pass(CheckId.POSITIVE_NET_EV, f"net_ev={ev.net_ev}")


def check_liquidity_sufficient(ctx: GateContext) -> CheckResult:
    """Enough resting notional on the side we would cross."""
    snapshot = ctx.snapshot
    if snapshot is None or ctx.min_liquidity_usdc is None:
        return _fail(CheckId.LIQUIDITY_SUFFICIENT, "no snapshot or no liquidity floor")
    if ctx.ev is None:
        return _fail(CheckId.LIQUIDITY_SUFFICIENT, "no EV assessment to locate the token")

    book = snapshot.book_for(ctx.ev.token_id)
    if book is None:
        return _fail(CheckId.LIQUIDITY_SUFFICIENT, "no book for token")
    notional = sum((level.price * level.size for level in book.asks), Decimal(0))
    if notional < ctx.min_liquidity_usdc:
        return _fail(
            CheckId.LIQUIDITY_SUFFICIENT,
            f"ask notional {notional} below floor {ctx.min_liquidity_usdc}",
        )
    return _pass(CheckId.LIQUIDITY_SUFFICIENT, f"ask notional {notional}")


def check_spread_acceptable(ctx: GateContext) -> CheckResult:
    snapshot = ctx.snapshot
    if snapshot is None or ctx.max_spread_bps is None or ctx.ev is None:
        return _fail(CheckId.SPREAD_ACCEPTABLE, "no snapshot, limit or EV assessment")
    book = snapshot.book_for(ctx.ev.token_id)
    if book is None or book.spread is None or not book.best_ask:
        return _fail(CheckId.SPREAD_ACCEPTABLE, "spread not measurable")
    spread_bps = book.spread / book.best_ask * Decimal(10_000)
    if spread_bps > ctx.max_spread_bps:
        return _fail(
            CheckId.SPREAD_ACCEPTABLE,
            f"spread {spread_bps:.0f}bps over {ctx.max_spread_bps}bps",
        )
    return _pass(CheckId.SPREAD_ACCEPTABLE, f"spread {spread_bps:.0f}bps")


def check_slippage_acceptable(ctx: GateContext) -> CheckResult:
    """Slippage as priced by the EV engine, not re-derived here.

    Deliberately reads the cost the EV figure was actually computed from. A gate
    that recomputes a cost can disagree with the number it is gating, and then the
    approval and the arithmetic are about different trades.
    """
    if ctx.ev is None or ctx.max_slippage_bps is None:
        return _fail(CheckId.SLIPPAGE_ACCEPTABLE, "no EV assessment or no limit")
    slippage = ctx.ev.costs.slippage_bps
    if slippage > ctx.max_slippage_bps:
        return _fail(
            CheckId.SLIPPAGE_ACCEPTABLE,
            f"slippage {slippage:.0f}bps over {ctx.max_slippage_bps}bps",
        )
    return _pass(CheckId.SLIPPAGE_ACCEPTABLE, f"slippage {slippage:.0f}bps")


def check_capital_available(ctx: GateContext) -> CheckResult:
    if ctx.capital_available_usdc is None or ctx.stake_usdc is None:
        return _fail(CheckId.CAPITAL_AVAILABLE, "capital or stake unknown")
    if ctx.stake_usdc <= 0:
        return _fail(CheckId.CAPITAL_AVAILABLE, f"non-positive stake {ctx.stake_usdc}")
    if ctx.stake_usdc > ctx.capital_available_usdc:
        return _fail(
            CheckId.CAPITAL_AVAILABLE,
            f"stake {ctx.stake_usdc} over available {ctx.capital_available_usdc}",
        )
    return _pass(CheckId.CAPITAL_AVAILABLE, f"stake {ctx.stake_usdc}")


def check_exposure_acceptable(ctx: GateContext) -> CheckResult:
    """The exposure tracker names the limit; this check only enforces the answer.

    ``exposure_breach`` is the one field where absent means passing, because the
    tracker returns the name of a breached limit or nothing. It is still not a
    silent pass: the tracker having never run is caught by the risk check, which
    cannot be approved without it.
    """
    if ctx.exposure_breach:
        return _fail(CheckId.EXPOSURE_ACCEPTABLE, ctx.exposure_breach)
    return _pass(CheckId.EXPOSURE_ACCEPTABLE)


def check_no_duplicate_order(ctx: GateContext) -> CheckResult:
    """Two orders on one intent is the cheapest way to double an intended position."""
    if ctx.duplicate_order_exists is None:
        return _fail(CheckId.NO_DUPLICATE_ORDER, "duplicate state unknown")
    if ctx.duplicate_order_exists:
        return _fail(CheckId.NO_DUPLICATE_ORDER, "an order already exists for this intent")
    return _pass(CheckId.NO_DUPLICATE_ORDER)


def check_execution_healthy(ctx: GateContext) -> CheckResult:
    if ctx.execution_healthy is None:
        return _fail(CheckId.EXECUTION_HEALTHY, "execution health unknown")
    if not ctx.execution_healthy:
        return _fail(CheckId.EXECUTION_HEALTHY, "execution path unhealthy")
    return _pass(CheckId.EXECUTION_HEALTHY)


def check_risk_approved(ctx: GateContext) -> CheckResult:
    if ctx.risk is None:
        return _fail(CheckId.RISK_APPROVED, "risk engine did not run")
    if not ctx.risk.approved:
        return _fail(CheckId.RISK_APPROVED, ctx.risk.reason or "risk declined")
    return _pass(CheckId.RISK_APPROVED)


def check_book_cleared_at_start(ctx: GateContext) -> CheckResult:
    """Refuse to enter into the window where the venue clears the book.

    The venue cancels outstanding limit orders at the official contest start,
    emptying the book -- and the guarantee is best-effort: a contest that starts
    early can leave a resting order live into play. So entering just before a start
    is a bet on the venue's timing rather than on the market.

    Passes when there is no contest start at all, which is the honest answer for a
    market that has none. It fails when a start exists and the clock does not, since
    "is it near the start" is unanswerable without one.
    """
    if ctx.contest_start is None:
        return _pass(CheckId.BOOK_CLEARED_AT_START, "no contest start")
    if ctx.now is None or ctx.no_entry_seconds_before_start is None:
        return _fail(CheckId.BOOK_CLEARED_AT_START, "contest start known but no clock or window")

    seconds_to_start = (ctx.contest_start - ctx.now).total_seconds()
    if 0 <= seconds_to_start <= ctx.no_entry_seconds_before_start:
        return _fail(
            CheckId.BOOK_CLEARED_AT_START,
            f"{seconds_to_start:.0f}s to contest start, inside the "
            f"{ctx.no_entry_seconds_before_start:.0f}s no-entry window",
        )
    return _pass(CheckId.BOOK_CLEARED_AT_START, f"{seconds_to_start:.0f}s to start")


def check_reference_feed_matched(ctx: GateContext) -> CheckResult:
    """The source we modelled from must be the source that settles the market.

    Crypto up/down markets settle on a Chainlink TWAP -- a 30-second lookback at
    five minutes, 60 at fifteen and four hours -- for both the price to beat and the
    settlement price. A model fed Binance spot is not slightly wrong: it is pricing
    a different instrument, and at a five-minute horizon the spot-to-TWAP basis is
    the entire edge.

    Passes when neither side names a feed, which is the case for every market that
    does not settle against a price at all. Fails when one names one and the other
    does not, because that asymmetry means somebody has not been asked.
    """
    modelled = ctx.model_reference_feed
    settles = ctx.settlement_reference_feed
    if modelled is None and settles is None:
        return _pass(CheckId.REFERENCE_FEED_MATCHED, "no reference feed involved")
    if modelled is None or settles is None:
        return _fail(
            CheckId.REFERENCE_FEED_MATCHED,
            f"one side unknown: modelled={modelled!r} settles={settles!r}",
        )
    if modelled != settles:
        return _fail(
            CheckId.REFERENCE_FEED_MATCHED,
            f"modelled on {modelled!r} but settles on {settles!r}",
        )
    return _pass(CheckId.REFERENCE_FEED_MATCHED, modelled)


#: The checklist, in evaluation order. Order is presentational only -- every check
#: runs regardless -- but it reads as the order a human would ask the questions in:
#: what is this market, what do we believe, what does it cost, may we act.
DEFAULT_CHECKS: tuple[tuple[CheckId, CheckFn], ...] = (
    (CheckId.MARKET_VALID, check_market_valid),
    (CheckId.RESOLUTION_VALID, check_resolution_valid),
    (CheckId.CLASSIFICATION_VALID, check_classification_valid),
    (CheckId.DATA_FRESH, check_data_fresh),
    (CheckId.MODEL_AVAILABLE, check_model_available),
    (CheckId.PROBABILITY_VALID, check_probability_valid),
    (CheckId.POSITIVE_NET_EV, check_positive_net_ev),
    (CheckId.LIQUIDITY_SUFFICIENT, check_liquidity_sufficient),
    (CheckId.SPREAD_ACCEPTABLE, check_spread_acceptable),
    (CheckId.SLIPPAGE_ACCEPTABLE, check_slippage_acceptable),
    (CheckId.BOOK_CLEARED_AT_START, check_book_cleared_at_start),
    (CheckId.REFERENCE_FEED_MATCHED, check_reference_feed_matched),
    (CheckId.CAPITAL_AVAILABLE, check_capital_available),
    (CheckId.EXPOSURE_ACCEPTABLE, check_exposure_acceptable),
    (CheckId.NO_DUPLICATE_ORDER, check_no_duplicate_order),
    (CheckId.EXECUTION_HEALTHY, check_execution_healthy),
    (CheckId.RISK_APPROVED, check_risk_approved),
)


def default_gate() -> SafetyGate:
    """A gate with every check registered.

    The only supported way to build one for production. ``SafetyGate`` accepts an
    arbitrary check list because tests need to isolate a single check, and an
    unregistered check already fails closed -- but a caller assembling its own list
    is one forgotten line away from a gate that approves what it never examined.
    """
    return SafetyGate(DEFAULT_CHECKS)
