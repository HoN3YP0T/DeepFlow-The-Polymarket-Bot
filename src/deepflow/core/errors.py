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
