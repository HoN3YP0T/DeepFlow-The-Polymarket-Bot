"""Execution engine.

The boundary between a risk-approved signal and the venue. Responsibilities:

* translate a signal plus a sizing result into an :class:`OrderIntent`
* choose the order type
* re-check the breakers immediately before submission
* route to the mode-appropriate executor (live, paper, shadow)

The pre-submission breaker re-check is deliberate. Between risk approval and
submission the book can move, the stream can drop, or a breaker can trip. The
gate's verdict was true when it was computed; this confirms it is still true at
the only moment that matters.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from deepflow.adapters.polymarket import venue
from deepflow.config.settings import Settings
from deepflow.core.domain import OrderIntent, OrderRecord, Signal
from deepflow.core.enums import OrderSide, OrderType, SignalAction, TimeInForce
from deepflow.core.errors import RiskRejectedError, TradingHaltedError
from deepflow.core.logging import get_logger
from deepflow.execution.idempotency import build_client_key
from deepflow.execution.order_manager import OrderManager
from deepflow.risk.circuit_breakers import CircuitBreakerRegistry
from deepflow.risk.sizing import SizingResult

log = get_logger(__name__)

BPS: Final = Decimal(10_000)

#: A contract cannot trade at 0 or 1; a limit at either boundary is rejected rather
#: than merely aggressive.
MIN_PRICE: Final = Decimal("0.001")
MAX_PRICE: Final = Decimal("0.999")

#: Urgency at or above which a MARKET order is considered -- and only considered,
#: because ``allow_market_orders`` still has to be on.
MARKET_ORDER_URGENCY: Final = Decimal("0.9")


class ExecutionEngine:
    """Builds and dispatches orders."""

    def __init__(
        self,
        *,
        order_manager: OrderManager,
        breakers: CircuitBreakerRegistry,
        settings: Settings,
    ) -> None:
        self._orders = order_manager
        self._breakers = breakers
        self._settings = settings

    def build_intent(
        self,
        signal: Signal,
        sizing: SizingResult,
        *,
        urgency: Decimal = Decimal(0),
        tick_size: Decimal | None = None,
        salt: str = "",
    ) -> OrderIntent:
        """Turn an approved signal into a concrete, venue-conforming order.

        Three things happen here that must not happen anywhere else.

        **The limit price is bounded by the slippage the gate approved.** The
        signal's target is the price the EV assessment was computed at; the limit
        is that target walked by at most ``max_slippage_bps``. A limit set at the
        target alone would rarely fill; a limit set without the bound is an
        unbounded order wearing a limit's name.

        **Prices and sizes are snapped to the venue's grid before submission**, and
        conservatively -- a BUY price rounds *down*, a size rounds *down*. Both
        directions are chosen so the adjustment can only make the order smaller or
        cheaper than intended. Submitting a sub-tick price is a definitive
        rejection, and a size rounded up is the venue's "not enough balance" error
        wearing a rounding problem's clothes.

        **The idempotency key is derived from the rounded intent**, not the raw
        one. Keying off pre-rounding values would let two intents that differ only
        below the tick produce two keys and therefore two orders for what the venue
        sees as the same thing.

        ``tick_size`` is the market's **price** tick, and it drives both roundings:
        the venue derives its size precision from the price tick rather than
        publishing them separately (``venue.PRECISION_BY_TICK``). Omitted, no
        rounding is applied -- deliberately, rather than assuming a default grid:
        guessing 0.01 for a market quoted in 0.001 rounds a valid price into a worse
        one.
        """
        price = self._limit_price(signal)
        if tick_size is not None:
            price = venue.round_price_to_tick(
                price, tick_size, side_is_buy=signal.action is SignalAction.BUY
            )

        # Shares are derived from the **rounded** price, not the raw one. Two reasons,
        # and the second is the subtle one: the order will transact at the rounded
        # price, so that is the price the stake has to be affordable at; and deriving
        # from the raw price makes the share count differ below the tick, which makes
        # two intents the venue sees as identical produce two idempotency keys.
        shares = self._shares(sizing, price)
        if tick_size is not None:
            shares = venue.round_shares(shares, tick_size)

        side = OrderSide.BUY if signal.action is SignalAction.BUY else OrderSide.SELL
        order_type = self.choose_order_type(signal, urgency=urgency)

        return OrderIntent(
            client_key=build_client_key(
                token_id=signal.token_id,
                side=side,
                price=price,
                size=shares,
                epoch_seconds=signal.generated_at.timestamp(),
                salt=salt,
            ),
            condition_id=signal.condition_id,
            token_id=signal.token_id,
            side=side,
            order_type=order_type,
            size_shares=shares,
            limit_price=price,
            max_slippage_bps=signal.ev.costs.slippage_bps,
            time_in_force=TimeInForce.GTC,
            # No GTD expiry. The venue expires a GTD order a minute early and
            # requires an expiration at least three minutes out, so the shortest
            # expressible lifetime is ~2 minutes against a 10-second timeout. A
            # working order is GTC plus our own cancel; see OrderManager.
            expires_at=None,
            # All-in collateral cap for a BUY, so a taker fee charged on top of the
            # notional cannot push the spend past what sizing allowed.
            max_spend=sizing.stake_usdc if side is OrderSide.BUY else None,
        )

    def _limit_price(self, signal: Signal) -> Decimal:
        """The worst price this order may transact at.

        The signal's target walked by the slippage the EV assessment already
        charged. Using the assessed slippage rather than a separate setting keeps
        the order and the arithmetic that approved it describing the same trade --
        a limit looser than the assessed cost would fill at a price the EV
        calculation never saw.
        """
        target = signal.target_price
        drift = target * (signal.ev.costs.slippage_bps / BPS)
        limit = target + drift if signal.action is SignalAction.BUY else target - drift
        # Clamped inside the unit interval: a contract cannot trade at 0 or 1, and a
        # limit at the boundary is rejected rather than merely aggressive.
        return min(max(limit, MIN_PRICE), MAX_PRICE)

    @staticmethod
    def _shares(sizing: SizingResult, price: Decimal) -> Decimal:
        """Shares affordable at ``price`` for the approved stake."""
        if price <= 0:
            return Decimal(0)
        return sizing.stake_usdc / price

    def choose_order_type(self, signal: Signal, *, urgency: Decimal) -> OrderType:
        """Select an order type.

        Default is ``MARKETABLE_LIMIT``: it takes liquidity like a market order
        but carries a worst-price bound, which on a thin outcome book is the
        difference between paying the spread and paying the whole book.

        A true ``MARKET`` order is permitted only when
        ``allow_market_orders`` is set and the situation justifies unbounded
        price risk -- in practice an emergency exit on a confirmed state
        change, where not getting out is the larger risk.

        ``urgency`` is the caller's statement of how badly this needs to happen,
        0 to 1. It can only ever *escalate* toward MARKET, and only with the
        setting on: a configuration that forbids market orders is not something
        urgency may override, because the situations that feel most urgent are
        exactly the ones where an unbounded order does the most damage.
        """
        if (
            urgency >= MARKET_ORDER_URGENCY
            and self._settings.thresholds.execution.allow_market_orders
        ):
            log.warning(
                "execution.market_order_chosen",
                condition_id=str(signal.condition_id),
                urgency=str(urgency),
            )
            return OrderType.MARKET
        return OrderType.MARKETABLE_LIMIT

    async def submit(
        self,
        signal: Signal,
        sizing: SizingResult,
        *,
        urgency: Decimal = Decimal(0),
        tick_size: Decimal | None = None,
    ) -> OrderRecord:
        """Execute an approved signal, re-checking the breakers first.

        The re-check is the point of this method existing rather than the caller
        calling the order manager. Between risk approval and submission the book
        can move, the stream can drop, or a breaker can trip; the gate's verdict
        was true when it was computed and this confirms it is still true at the
        only moment that matters.

        A tripped breaker raises rather than returning a rejected record. It is not
        an order outcome -- nothing was submitted -- and returning a record would
        put a fictional order into the journal and the reconciler's view.
        """
        if not self._breakers.entries_allowed():
            open_reasons = self._breakers.open_reasons
            log.warning(
                "execution.blocked_by_breaker",
                condition_id=str(signal.condition_id),
                reasons=[str(reason) for reason in open_reasons],
            )
            # The first open reason, with the rest as detail. The error carries one
            # reason because a halt has one primary cause to act on, and listing all
            # of them in the detail keeps the others visible for the journal.
            raise TradingHaltedError(
                open_reasons[0],
                detail=", ".join(str(reason) for reason in open_reasons[1:]),
            )

        intent = self.build_intent(signal, sizing, urgency=urgency, tick_size=tick_size)
        if intent.size_shares <= 0:
            raise RiskRejectedError(
                f"intent sized to zero shares at {intent.limit_price} (stake {sizing.stake_usdc})"
            )

        log.info(
            "execution.submitting",
            client_key=str(intent.client_key),
            condition_id=str(signal.condition_id),
            order_type=str(intent.order_type),
            size_shares=str(intent.size_shares),
            limit_price=str(intent.limit_price),
        )
        return await self._orders.execute(intent)
