"""Exception hierarchy.

Everything derives from :class:`DeepFlowError` so the supervisor can
distinguish "our own invariant broke" from an arbitrary third-party crash.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from deepflow.core.enums import BreakerReason
    from deepflow.core.state_machine import MarketState


class DeepFlowError(Exception):
    """Base class for all DeepFlow errors."""


# --- Configuration --------------------------------------------------------
class ConfigurationError(DeepFlowError):
    """Settings are missing, malformed, or mutually inconsistent."""


class LiveModeNotConfirmedError(ConfigurationError):
    """LIVE mode was requested without the required explicit confirmation."""


# --- State ----------------------------------------------------------------
class IllegalTransitionError(DeepFlowError):
    """An attempt was made to move a market along an edge that does not exist."""

    def __init__(self, *, source: MarketState, target: MarketState) -> None:
        self.source = source
        self.target = target
        super().__init__(f"illegal transition {source} -> {target}")


# --- Data -----------------------------------------------------------------
class DataError(DeepFlowError):
    """Base for data-quality problems."""


class StaleDataError(DataError):
    """Market state is older than the configured freshness budget."""


class InconsistentDataError(DataError):
    """Internally contradictory state, e.g. a crossed book or bid > ask."""


# --- Adapters -------------------------------------------------------------
class AdapterError(DeepFlowError):
    """Base for failures originating in an external integration."""


class PolymarketApiError(AdapterError):
    """A Polymarket REST call failed."""


class StreamDisconnectedError(AdapterError):
    """A WebSocket subscription dropped and has not yet recovered."""


class RateLimitedError(AdapterError):
    """HTTP 429. Back off; the request itself was well-formed."""


# --- Venue trading modes --------------------------------------------------
# These are documented, expected conditions rather than faults, and they are
# separated from ``PolymarketApiError`` for that reason: treating an announced
# maintenance restart as an API failure trips the API_FAILURE breaker and
# latches the system off for a two-minute window it should simply have waited
# out. See docs/ADR-0003 and /trading/matching-engine.
class VenueModeError(AdapterError):
    """Base for a venue state that restricts, but does not forbid, trading."""


class MatchingEngineRestartingError(VenueModeError):
    """HTTP 425. The matching engine is restarting.

    Retry with exponential backoff from 1-2s. Cancels are still accepted, and
    for two minutes after the restart only ``postOnly`` orders are.
    """


class PostOnlyModeRequiredError(VenueModeError):
    """HTTP 503 ``post_only_mode``. Only cancels and post-only orders accepted.

    Carries the venue's own ``retry_after_seconds`` when supplied. A taking
    order must not be resubmitted unchanged -- either wait out the window or
    convert the intent to a resting maker order, which is a different trade and
    so a decision for the strategy, not the adapter.
    """

    def __init__(self, *, retry_after_seconds: float | None = None) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            "venue in post-only mode"
            + (f"; retry after {retry_after_seconds}s" if retry_after_seconds else "")
        )


class CancelOnlyModeError(VenueModeError):
    """HTTP 503 cancel-only. New orders refused; cancels still accepted."""


class ClosedOnlyModeError(VenueModeError):
    """Account-level restriction: only position-reducing orders are accepted.

    Checked before placing an entry so the refusal is legible, rather than
    arriving as a generic rejection on every new position while exits keep
    working.
    """


# --- Execution ------------------------------------------------------------
class ExecutionError(DeepFlowError):
    """Base for order-lifecycle failures."""


class OrderRejectedError(ExecutionError):
    """The venue refused the order outright. Safe to treat as not-executed."""


class ExecutionUncertainError(ExecutionError):
    """Submission outcome is indeterminate -- a timeout, a dropped connection.

    This is the dangerous case. The order may or may not be live. Never retry
    on this error; hand off to reconciliation to establish ground truth first.
    """


class DuplicateOrderError(ExecutionError):
    """An order with this idempotency key is already in flight."""


class TradeSettlementFailedError(ExecutionError):
    """A matched trade later failed to settle on chain.

    A fill is not final when it matches. The user stream reports a trade moving
    ``MATCHED -> MINED -> CONFIRMED``, and it can instead go ``RETRYING`` or
    ``FAILED``. Booking a position on ``MATCHED`` and never revisiting it
    produces a phantom holding the venue does not believe in -- which is
    precisely the divergence reconciliation exists to catch, arriving through
    the fast path that was supposed to be authoritative.
    """


class ReconciliationError(DeepFlowError):
    """Local state and venue state disagree and could not be reconciled."""


# --- Risk / safety --------------------------------------------------------
class RiskRejectedError(DeepFlowError):
    """The risk engine refused to approve a trade."""

    def __init__(self, reason: str, *, detail: dict[str, Any] | None = None) -> None:
        self.reason = reason
        self.detail = detail or {}
        super().__init__(reason)


class TradingHaltedError(DeepFlowError):
    """A circuit breaker is open; new entries are refused."""

    def __init__(self, reason: BreakerReason, *, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"trading halted: {reason}{f' ({detail})' if detail else ''}")
