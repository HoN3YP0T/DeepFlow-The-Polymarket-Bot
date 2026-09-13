"""Venue facts, as published by Polymarket.

Everything in this module is a *rule of the exchange*, not a tunable of ours.
It is separated from :mod:`deepflow.config.thresholds` for that reason: a
threshold is an opinion we can sweep in a backtest, whereas a tick-size grid or
a fee formula is arithmetic the venue will reject us for getting wrong.

Sourced from https://docs.polymarket.com (verified 2026-09-12), page by page:

* ``/trading/fees`` and ``/market-data/market-details#trading-fees`` -- fee formula,
  per-category rates, 5-decimal rounding
* ``/market-data/market-details#trading-constraints`` -- tick-size grid
* ``/trading/place-orders`` -- price/size/amount precision, GTD rules, FAK/FOK,
  post-only, market-BUY amounts denominated in collateral
* ``/trading/matching-engine`` -- HTTP 425, cancel-only and post-only modes
* ``/resources/error-codes`` -- status-code semantics
* ``/trading/manage-orders#order-heartbeats`` -- dead-man's-switch cadence
* ``/resources/contracts`` and ``/concepts/pusd`` -- addresses, collateral token
* ``/market-data/realtime-data`` -- socket endpoints and heartbeat cadences

Layering note: this module imports no SDK and no other DeepFlow package, so
unlike the rest of ``adapters/polymarket`` it is safe to import from an engine.
The venue's fee formula is an input to expected value, and routing it through a
port would only obscure that it is fixed arithmetic rather than a model choice.

Re-verify after any venue changelog entry (``/changelog/predictions``). A stale
constant here is silently wrong, which is the expensive kind.
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Final

# --- Chain and contracts --------------------------------------------------
POLYGON_CHAIN_ID: Final = 137

CTF_EXCHANGE: Final = "0xE111180000d2663C0091e4f400237545B87B996B"
NEG_RISK_CTF_EXCHANGE: Final = "0xe2222d279d744050d28e00520010520000310F59"
CONDITIONAL_TOKENS: Final = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
PUSD_COLLATERAL: Final = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
COLLATERAL_ONRAMP: Final = "0x93070a847efEf7F70739046A929D47a521F5B8ee"
COLLATERAL_OFFRAMP: Final = "0x2957922Eb93258b93368531d39fAcCA3B4dC5854"
USDC_E_POLYGON: Final = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"

EXCHANGE_EIP712_DOMAIN_NAME: Final = "Polymarket CTF Exchange"
EXCHANGE_EIP712_DOMAIN_VERSION: Final = "2"


def exchange_for(*, negative_risk: bool) -> str:
    """Verifying contract for an order's EIP-712 signature.

    Signing against the wrong exchange is a silent, total failure: the
    signature verifies against a domain the venue is not checking, and the
    order is rejected with no hint that ``neg_risk`` was the cause. The flag
    must come from the market/book payload, never from a guess about the
    question's shape.
    """
    return NEG_RISK_CTF_EXCHANGE if negative_risk else CTF_EXCHANGE


#: Collateral is **pUSD**, an ERC-20 wrapper over USDC with 6 decimals -- not
#: USDC itself. Balances, order notionals and fees are all quoted in it.
COLLATERAL_SYMBOL: Final = "pUSD"
COLLATERAL_DECIMALS: Final = 6

#: On-wire integer scaling for ``makerAmount``/``takerAmount``.
AMOUNT_SCALE: Final = Decimal(10) ** COLLATERAL_DECIMALS


# --- Sockets --------------------------------------------------------------
MARKET_WS_URL: Final = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
SPORTS_WS_URL: Final = "wss://sports-api.polymarket.com/ws"
RTDS_WS_URL: Final = "wss://ws-live-data.polymarket.com"
CLOB_REST_URL: Final = "https://clob.polymarket.com"

#: Application-level heartbeats, per feed. These are *not* uniform, and a
#: single shared ping interval silently drops the strictest feed: the sports
#: socket closes the connection if a ``pong`` does not follow its ``ping``
#: within 10 seconds, while the market socket expects us to drive the ``PING``.
MARKET_WS_PING_INTERVAL_SECONDS: Final = 10.0
"""Client sends the text frame ``PING`` every 10s; server replies ``PONG``."""
RTDS_WS_PING_INTERVAL_SECONDS: Final = 5.0
"""Client sends ``PING`` every 5s."""
SPORTS_WS_PONG_DEADLINE_SECONDS: Final = 10.0
"""Server sends lowercase ``ping`` every 5s; we must reply ``pong`` inside 10s."""


# --- Tick size grid -------------------------------------------------------
#: Permitted minimum price increments. ``0.0025`` is documented as applying only
#: to World Cup *to advance* / moneyline / spreads / totals markets; it is still
#: listed here because the venue's instruction is to read the active value from
#: the market rather than infer it from the category.
TICK_SIZES: Final = (
    Decimal("0.1"),
    Decimal("0.01"),
    Decimal("0.005"),
    Decimal("0.0025"),
    Decimal("0.001"),
    Decimal("0.0001"),
)

#: tick size -> (price decimals, size decimals, amount decimals).
#: Note that the table is not monotonic in the tick: ``0.005`` and ``0.001``
#: share a precision row, as do ``0.0025`` and ``0.0001``. Deriving precision
#: from ``-tick.as_tuple().exponent`` gets ``0.005`` and ``0.0025`` wrong.
PRECISION_BY_TICK: Final[dict[Decimal, tuple[int, int, int]]] = {
    Decimal("0.1"): (1, 2, 3),
    Decimal("0.01"): (2, 2, 4),
    Decimal("0.005"): (3, 2, 5),
    Decimal("0.0025"): (4, 2, 6),
    Decimal("0.001"): (3, 2, 5),
    Decimal("0.0001"): (4, 2, 6),
}

#: A price must sit strictly inside the open interval; 0 and 1 are not tradeable.
MIN_PRICE: Final = Decimal("0.0001")
MAX_PRICE: Final = Decimal("0.9999")


def _quantum(decimals: int) -> Decimal:
    return Decimal(1).scaleb(-decimals)


def _decimal_places(value: Decimal) -> int:
    """Decimal places in ``value``, treating NaN/Infinity as unbounded.

    ``Decimal.as_tuple().exponent`` is a string sentinel for those, and a
    non-finite amount must not slip through a precision check as though it were
    exact.
    """
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise ValueError(f"non-finite amount: {value}")
    return max(-exponent, 0)


def precision_for_tick(tick_size: Decimal) -> tuple[int, int, int]:
    """``(price, size, amount)`` decimal places for ``tick_size``."""
    try:
        return PRECISION_BY_TICK[Decimal(tick_size)]
    except KeyError:
        raise ValueError(f"unsupported tick size: {tick_size}") from None


def conforms_to_tick(price: Decimal, tick_size: Decimal) -> bool:
    """Whether ``price`` sits on the market's price grid."""
    return price % Decimal(tick_size) == 0


def round_price_to_tick(price: Decimal, tick_size: Decimal, *, side_is_buy: bool) -> Decimal:
    """Snap ``price`` onto the tick grid, conservatively for the given side.

    Rounding direction is chosen so the adjustment can never make the trade
    worse than intended: a BUY rounds down and a SELL rounds up, so the
    conforming price is always at least as good as the requested one. Rounding
    to nearest would let a sub-tick price silently cross further into the book.
    """
    tick = Decimal(tick_size)
    price_decimals, _, _ = precision_for_tick(tick)
    rounding = ROUND_DOWN if side_is_buy else ROUND_CEILING
    snapped = (price / tick).to_integral_value(rounding=rounding) * tick
    snapped = snapped.quantize(_quantum(price_decimals))
    return min(max(snapped, MIN_PRICE), MAX_PRICE)


def round_shares(size: Decimal, tick_size: Decimal) -> Decimal:
    """Round a share quantity **down** to the market's size precision.

    Always down: rounding up can push the signed notional past the collateral
    actually available, which the venue reports as ``not enough balance /
    allowance`` rather than as a rounding problem.
    """
    _, size_decimals, _ = precision_for_tick(tick_size)
    return Decimal(size).quantize(_quantum(size_decimals), rounding=ROUND_DOWN)


def round_notional(amount: Decimal, tick_size: Decimal) -> Decimal:
    """Round a collateral amount per the venue's two-step rule.

    Documented procedure: if the value exceeds *amount decimals*, round it up
    to ``amount decimals + 4`` first, then down to *amount decimals*. The
    intermediate step exists to stop a binary-float style representation error
    (``5.199999...``) from losing a whole unit in the final truncation.
    """
    _, _, amount_decimals = precision_for_tick(tick_size)
    value = Decimal(amount)
    if _decimal_places(value) <= amount_decimals:
        return value.quantize(_quantum(amount_decimals))
    widened = value.quantize(_quantum(amount_decimals + 4), rounding=ROUND_CEILING)
    return widened.quantize(_quantum(amount_decimals), rounding=ROUND_DOWN)


def to_amount_integer(amount: Decimal) -> int:
    """Encode a collateral amount as its 6-decimal on-wire integer."""
    return int((Decimal(amount) * AMOUNT_SCALE).to_integral_value(rounding=ROUND_DOWN))


# --- Fees -----------------------------------------------------------------
#: Fees are rounded to 5 decimal places; anything smaller is not charged.
FEE_DECIMALS: Final = 5
FEE_QUANTUM: Final = Decimal(1).scaleb(-FEE_DECIMALS)

#: Published per-category taker rates. These are a *fallback for planning only*.
#: The authoritative values are ``market.trading.fee_schedule`` on the market
#: itself; a market that has been recategorised or repriced will disagree with
#: this table and the market wins.
TAKER_FEE_RATE_BY_CATEGORY: Final[dict[str, Decimal]] = {
    "crypto": Decimal("0.07"),
    "sports": Decimal("0.05"),
    "finance": Decimal("0.04"),
    "politics": Decimal("0.04"),
    "economics": Decimal("0.05"),
    "culture": Decimal("0.05"),
    "weather": Decimal("0.05"),
    "mentions": Decimal("0.04"),
    "tech": Decimal("0.04"),
    "geopolitics": Decimal("0"),
    "other": Decimal("0.05"),
}

#: Makers are never charged. Only the taker pays.
MAKER_FEE_RATE: Final = Decimal("0")

#: Only exponent 1 has been observed, and it is the value that reproduces the
#: published fee tables. It is read from the market rather than hardcoded
#: because the venue exposes it as a per-market parameter.
DEFAULT_FEE_EXPONENT: Final = 1


def taker_fee(
    *,
    shares: Decimal,
    price: Decimal,
    rate: Decimal,
    exponent: Decimal | int | float = DEFAULT_FEE_EXPONENT,
) -> Decimal:
    """Taker fee in collateral: ``shares * rate * (p * (1 - p)) ** exponent``.

    The shape is the thing to internalise, not the number. The fee is quoted
    against ``p * (1 - p)``, so it is *symmetric about 0.50* and peaks there --
    a fill at 0.30 costs the same in collateral as one at 0.70. That makes it
    cheap in absolute terms in the 0.85-0.98 band this system trades, and
    expensive *relative to the edge*: at 0.95 a 4% -rate market charges
    0.19 collateral per 100 shares, which is 0.19c per share against a
    per-share edge measured in single cents.
    """
    p = Decimal(price)
    if p <= 0 or p >= 1:
        return Decimal(0)
    base = p * (Decimal(1) - p)
    if exponent != 1:
        base = Decimal(str(float(base) ** float(exponent)))
    fee = Decimal(shares) * Decimal(rate) * base
    return fee.quantize(FEE_QUANTUM, rounding=ROUND_HALF_UP)


def taker_fee_bps(
    *, price: Decimal, rate: Decimal, exponent: Decimal | int | float = DEFAULT_FEE_EXPONENT
) -> Decimal:
    """Taker fee as basis points of notional, for the EV cost stack.

    Per-share fee divided by per-share price: ``rate * (1 - p)`` at exponent 1.
    Expressed in bps because that is the unit :class:`CostBreakdown` uses, and
    because the bps figure is the one that is *not* symmetric -- the same
    collateral fee is a far larger fraction of a 0.05 contract than of a 0.95
    one.
    """
    p = Decimal(price)
    if p <= 0 or p >= 1:
        return Decimal(0)
    per_share = taker_fee(shares=Decimal(1), price=p, rate=rate, exponent=exponent)
    return (per_share / p) * Decimal(10_000)


# --- Order lifetime rules -------------------------------------------------
#: GTD orders expire one minute *before* their stated expiration, as a venue-side
#: security threshold. An effective lifetime of N seconds needs ``now + 60 + N``.
GTD_SECURITY_MARGIN_SECONDS: Final = 60
#: The submitted expiration must be at least 3 minutes out or the order is
#: rejected, so the shortest *effective* GTD lifetime is about two minutes.
GTD_MIN_EXPIRATION_SECONDS: Final = 180
GTD_MIN_EFFECTIVE_LIFETIME_SECONDS: Final = GTD_MIN_EXPIRATION_SECONDS - GTD_SECURITY_MARGIN_SECONDS


def gtd_expiration(*, now_epoch_seconds: float, lifetime_seconds: float) -> int:
    """Expiration timestamp for a GTD order with the requested lifetime.

    Raises when the requested lifetime is below what the venue permits. That is
    deliberate: a sub-two-minute working order is not expressible as GTD, and
    silently stretching it to the minimum would leave an order resting far
    longer than the strategy intended. Short-lived working orders must be GTC
    plus an explicit client-side cancel.
    """
    if lifetime_seconds < GTD_MIN_EFFECTIVE_LIFETIME_SECONDS:
        raise ValueError(
            f"GTD lifetime {lifetime_seconds}s is below the venue minimum of "
            f"{GTD_MIN_EFFECTIVE_LIFETIME_SECONDS}s; use GTC with a client-side cancel"
        )
    return int(now_epoch_seconds + GTD_SECURITY_MARGIN_SECONDS + lifetime_seconds)


#: After every matching-engine restart the venue accepts only cancels and
#: ``postOnly`` orders for two minutes.
POST_ONLY_WINDOW_AFTER_RESTART_SECONDS: Final = 120

#: Order-heartbeat dead-man's switch: send every 5s, orders are cancelled if no
#: valid heartbeat arrives within 10s, and the sweep runs every 5s (so
#: cancellation can lag the timeout by up to 5s).
#: The CLOB route for the order dead-man's switch. Unwrapped by the SDK, whose only
#: heartbeats are WebSocket keepalives, so it is posted through the authenticated
#: transport directly (finding 72). Note the ``/v1`` prefix: most CLOB routes in this
#: SDK are unversioned, and this one is not.
ORDER_HEARTBEAT_PATH: Final = "/v1/heartbeats"

ORDER_HEARTBEAT_SEND_INTERVAL_SECONDS: Final = 5.0
ORDER_HEARTBEAT_TIMEOUT_SECONDS: Final = 10.0
ORDER_HEARTBEAT_SWEEP_INTERVAL_SECONDS: Final = 5.0


# --- HTTP status semantics ------------------------------------------------
#: Statuses on which a *retry of the same request* is correct.
RETRYABLE_STATUSES: Final = frozenset({425, 429, 500, 503})

#: Matching engine restarting. Not an error -- back off from 1-2s and resume.
STATUS_ENGINE_RESTARTING: Final = 425
STATUS_RATE_LIMITED: Final = 429
#: Exchange paused, or placement blocked by cancel-only / post-only mode.
STATUS_UNAVAILABLE: Final = 503

#: ``code`` returned in a 503 body when the engine is in post-only mode.
POST_ONLY_MODE_CODE: Final = "post_only_mode"


def is_definitive_rejection(status: int) -> bool:
    """Whether ``status`` proves the order did *not* execute.

    This is the load-bearing distinction in the whole execution path. A 400 or
    404 is a refusal we can safely retry after fixing the request; a timeout or
    a 5xx is *uncertain* and must go to reconciliation, never to a retry.

    ``500 order timed out`` is the documented exception -- the venue states the
    order was rejected before reaching the book -- but it is deliberately not
    special-cased here, because distinguishing it relies on matching an error
    string, and being wrong about it means a duplicate position.
    """
    return status in (400, 401, 404)
