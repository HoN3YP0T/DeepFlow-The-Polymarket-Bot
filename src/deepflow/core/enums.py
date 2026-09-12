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
    """DeepFlow's *intent* vocabulary, which is narrower than the venue's.

    Mapped onto venue order types in ``adapters.polymarket.execution``:
    ``LIMIT`` -> a GTC limit, ``MARKETABLE_LIMIT`` -> a limit priced through the
    book (still a limit, so the worst price is bounded), ``MARKET`` -> a venue
    market order with ``FAK``/``FOK``.
    """

    LIMIT = "LIMIT"
    """Passive limit, rests on the book."""
    MARKETABLE_LIMIT = "MARKETABLE_LIMIT"
    """Crosses the spread but carries a worst-price bound. The default for
    taking liquidity -- a true market order has no slippage ceiling."""
    MARKET = "MARKET"
    """Unbounded. Only permitted where explicitly justified (e.g. emergency
    exit on a genuine state change)."""


class TimeInForce(StrEnum):
    """Venue order lifetimes.

    ``GTD`` is not usable for short-lived working orders: the venue requires the
    expiration to be at least three minutes out and expires the order a minute
    early, so the shortest expressible lifetime is about two minutes. A 10-second
    working order is ``GTC`` plus a client-side cancel. See
    ``adapters.polymarket.venue.gtd_expiration``.
    """

    GTC = "GTC"
    """Good till cancelled."""
    GTD = "GTD"
    """Good till date. Minimum ~2 minutes of effective life."""
    FAK = "FAK"
    """Fill and kill. Takes what is available, cancels the remainder. The venue
    default for market orders."""
    FOK = "FOK"
    """Fill or kill. All of it immediately, or none of it."""


class OrderStatus(StrEnum):
    PENDING_NEW = "PENDING_NEW"
    OPEN = "OPEN"
    DELAYED = "DELAYED"
    """Accepted by the venue but not yet matched, because the market imposes a
    matching delay (``market.trading.seconds_delay``). Filled amounts are zero
    and no trade ids exist yet, so this is a *pending* order and must not be
    read as a partial fill or as a rejection. Common on sports markets."""
    MATCHED_UNSETTLED = "MATCHED_UNSETTLED"
    """Matched, but settlement has not confirmed on chain. The position is
    probable, not real: a matched trade can still end up FAILED."""
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    """Submission outcome indeterminate. Must be resolved by reconciliation
    before any retry -- never re-send an order in this state."""


class TradeSettlementStatus(StrEnum):
    """On-chain settlement lifecycle of a fill, from the user stream.

    Kept distinct from :class:`OrderStatus` because the two answer different
    questions: the order status says whether the venue accepted and matched us,
    while this says whether the resulting transfer actually happened. Only
    ``CONFIRMED`` justifies treating a position as settled.
    """

    MATCHED = "MATCHED"
    MATCHED_NOT_BROADCASTED = "MATCHED_NOT_BROADCASTED"
    MINED = "MINED"
    CONFIRMED = "CONFIRMED"
    RETRYING = "RETRYING"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in (TradeSettlementStatus.CONFIRMED, TradeSettlementStatus.FAILED)

    @property
    def is_settled(self) -> bool:
        return self is TradeSettlementStatus.CONFIRMED


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
    SETTLEMENT_FAILURE = auto()
    """A matched trade failed to settle on chain. Distinct from an execution
    error: the venue accepted and matched the order, so retrying the submission
    is exactly the wrong response."""


class ComponentHealth(StrEnum):
    UP = "UP"
    DEGRADED = "DEGRADED"
    DOWN = "DOWN"
    UNKNOWN = "UNKNOWN"
