"""Enumerations shared across the system."""

from __future__ import annotations

from enum import StrEnum, auto


class RunMode(StrEnum):
    """Execution mode. The promotion path is BACKTEST -> PAPER -> SHADOW -> LIVE."""

    BACKTEST = "BACKTEST"
    """Replay historical data. No connections to live venues."""
    PAPER = "PAPER"
    """Live data, simulated fills, no orders sent."""
    SHADOW = "SHADOW"
    """Live data, orders fully built and validated, submission suppressed at
    the last hop. Proves the execution path without risking capital."""
    LIVE = "LIVE"
    """Real orders. Disabled by default; requires explicit confirmation."""


class MarketCategory(StrEnum):
    """Top-level market classification. Drives which probability engine runs."""

    FOOTBALL = "FOOTBALL"
    CRICKET = "CRICKET"
    TENNIS = "TENNIS"
    BADMINTON = "BADMINTON"
    OTHER_SPORTS = "OTHER_SPORTS"
    BTC_5M = "BTC_5M"
    CRYPTO = "CRYPTO"
    POLITICS = "POLITICS"
    GEOPOLITICS = "GEOPOLITICS"
    WAR_CONFLICT = "WAR_CONFLICT"
    CEASEFIRE = "CEASEFIRE"
    MILITARY_DIPLOMATIC = "MILITARY_DIPLOMATIC"
    OTHER = "OTHER"
    UNKNOWN = "UNKNOWN"
    """Classifier could not reach its confidence threshold. Never auto-traded."""


class OutcomeSide(StrEnum):
    YES = "YES"
    NO = "NO"


class OrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    LIMIT = "LIMIT"
    """Passive limit, rests on the book."""
    MARKETABLE_LIMIT = "MARKETABLE_LIMIT"
    """Crosses the spread but carries a worst-price bound. The default for
    taking liquidity -- a true market order has no slippage ceiling."""
    MARKET = "MARKET"
    """Unbounded. Only permitted where explicitly justified (e.g. emergency
    exit on a genuine state change)."""


class OrderStatus(StrEnum):
    PENDING_NEW = "PENDING_NEW"
    OPEN = "OPEN"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    """Submission outcome indeterminate. Must be resolved by reconciliation
    before any retry -- never re-send an order in this state."""


class ResolutionValidity(StrEnum):
    VALID = "VALID"
    AMBIGUOUS = "AMBIGUOUS"
    UNPARSEABLE = "UNPARSEABLE"
    UNSUPPORTED_SOURCE = "UNSUPPORTED_SOURCE"
    NOT_CHECKED = "NOT_CHECKED"


class ExitAction(StrEnum):
    HOLD = "HOLD"
    ADD = "ADD"
    PARTIAL_EXIT = "PARTIAL_EXIT"
    FULL_EXIT = "FULL_EXIT"
    EMERGENCY_EXIT = "EMERGENCY_EXIT"


class SignalAction(StrEnum):
    BUY = "BUY"
    SELL = "SELL"
    NO_TRADE = "NO_TRADE"


class DataQuality(StrEnum):
    FRESH = "FRESH"
    DEGRADED = "DEGRADED"
    """Usable for monitoring and exits, but blocks new entries."""
    STALE = "STALE"
    INCONSISTENT = "INCONSISTENT"


class BreakerReason(StrEnum):
    """Why new entries are halted. Mirrors the circuit-breaker catalogue."""

    STALE_DATA = auto()
    WEBSOCKET_FAILURE = auto()
    API_FAILURE = auto()
    EXECUTION_ERRORS = auto()
    ABNORMAL_SLIPPAGE = auto()
    DATABASE_FAILURE = auto()
    RECONCILIATION_FAILURE = auto()
    MODEL_FAILURE = auto()
    INVALID_RESOLUTION_STATE = auto()
    DAILY_LOSS_LIMIT = auto()
    EXCESSIVE_DRAWDOWN = auto()
    ABNORMAL_MARKET_BEHAVIOUR = auto()
    MANUAL_HALT = auto()


class ComponentHealth(StrEnum):
    UP = "UP"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"
