"""PAPER and SHADOW executors."""

from __future__ import annotations

from decimal import Decimal
from typing import Final

from deepflow.adapters.polymarket import venue
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import (
    BookLevel,
    FeeSchedule,
    MarketSnapshot,
    OrderIntent,
    OrderRecord,
)
from deepflow.core.enums import OrderSide, OrderStatus, RunMode
from deepflow.core.logging import get_logger
from deepflow.core.types import ZERO, ClobTokenId, OrderId
from deepflow.modes.base import ModeExecutor

log = get_logger(__name__)

#: Whether a **resting** (non-crossing) order is ever simulated as filling.
#:
#: It is not. A resting order fills only when a counterparty arrives and chooses to
#: cross to us, which a book snapshot cannot tell us: we would be the last arrival at
#: our price, behind the depth already there, and how much of that queue clears before
#: the price moves away is exactly the unknown. Modelling it as "no fill" is the
#: pessimistic reading, and pessimism is the only thing that makes a paper result
#: worth anything -- an optimistic queue assumption shows up later as a strategy that
#: was never profitable rather than as a bug in the simulator.
#:
#: Note the distinction this rests on: queue position applies to orders resting on
#: **our own** side of the book. Depth on the *opposite* side is ours to take, all of
#: it, at any price our limit permits -- there is no queue in front of a taker.
RESTING_ORDERS_FILL: Final = False


class PaperExecutor(ModeExecutor):
    """Simulates fills against the live book.

    The simulation must be pessimistic to be useful. Filling at the mid, or
    filling the whole size at the touch, produces paper results the live system
    cannot reproduce -- and the gap shows up as a strategy that was never
    profitable rather than as a bug.

    So: walk the book for the real size, assume we are behind the existing
    queue at our price level, and model partial fills rather than assuming
    completion.

    Three rules, each chosen so paper cannot flatter live:

    1. **Never fill better than the touch.** A crossing order pays the
       volume-weighted price of the levels it consumes, computed by the same
       :meth:`~deepflow.core.domain.OrderBook.vwap_to_fill` the EV engine used, so
       the simulated cost and the assessed cost are the same number rather than two
       estimates that happen to be close.
    2. **Partial-fill when depth runs out**, rather than assuming the remainder.
       This is also the only way the partial-fill path in reconciliation and
       position accounting gets exercised at all before live.
    3. **Charge the same taker fee the live path would pay**, from the market's own
       schedule. A paper run with no fee is optimistic by the whole fee, which in
       the 0.85-0.98 band is a large fraction of the edge.

    Without a book for the token the answer is a **rejection**, not a fill. A paper
    fill invented with no market data is the one output that would make a paper run
    actively misleading.
    """

    mode = RunMode.PAPER

    def __init__(self, clock: Clock | None = None) -> None:
        self._snapshots: dict[ClobTokenId, MarketSnapshot] = {}
        self._fees: dict[ClobTokenId, FeeSchedule] = {}
        self._clock = clock or SystemClock()
        self._sequence = 0

    def observe(
        self,
        snapshot: MarketSnapshot,
        *,
        fee_schedule: FeeSchedule | None = None,
    ) -> None:
        """Record the book a later fill will be simulated against.

        Keyed per token rather than per market: an intent names a token, and a
        market snapshot holds a book for each outcome.
        """
        for book in snapshot.books:
            self._snapshots[book.token_id] = snapshot
            if fee_schedule is not None:
                self._fees[book.token_id] = fee_schedule

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        """Simulate ``intent`` against the most recent observed book."""
        snapshot = self._snapshots.get(intent.token_id)
        book = None if snapshot is None else snapshot.book_for(intent.token_id)
        if book is None:
            log.warning("paper.no_book", token_id=str(intent.token_id))
            return self._record(
                intent,
                status=OrderStatus.REJECTED,
                error="no observed book for token; refusing to invent a fill",
            )

        levels = book.asks if intent.side is OrderSide.BUY else book.bids
        fillable = self._fillable_shares(intent, levels)

        if fillable <= 0:
            # Priced away from the book, so the order rests. That is a real and common
            # outcome rather than an error, so it sits OPEN rather than REJECTED.
            return self._record(intent, status=OrderStatus.OPEN)

        vwap = book.vwap_to_fill(fillable, side=intent.side)
        if vwap is None:
            # Depth accounted above says this size is available, so a None here means
            # the two disagree -- do not paper over it with a fabricated price.
            log.warning(
                "paper.vwap_unavailable",
                token_id=str(intent.token_id),
                shares=str(fillable),
            )
            return self._record(intent, status=OrderStatus.OPEN)

        status = (
            OrderStatus.FILLED if fillable >= intent.size_shares else OrderStatus.PARTIALLY_FILLED
        )
        log.info(
            "paper.filled",
            token_id=str(intent.token_id),
            status=str(status),
            requested=str(intent.size_shares),
            filled=str(fillable),
            vwap=str(vwap),
            fee=str(self._fee(intent.token_id, shares=fillable, price=vwap)),
        )
        return self._record(
            intent,
            status=status,
            filled_shares=fillable,
            average_fill_price=vwap,
        )

    def _fillable_shares(self, intent: OrderIntent, levels: tuple[BookLevel, ...]) -> Decimal:
        """How much of ``intent`` the observed book would actually fill.

        Two cases, and conflating them is the mistake this method was rewritten to
        fix:

        * **Crossing.** A BUY whose limit reaches the best ask is a taker, and a taker
          has no queue in front of it: every ask at or below the limit is available,
          inclusive of the limit itself. Bounded only by depth.
        * **Resting.** A BUY below the best ask joins the bid side and waits for a
          counterparty to cross to *us*. A snapshot cannot say whether that happens,
          so it is simulated as no fill -- see :data:`RESTING_ORDERS_FILL`.
        """
        if not levels:
            return ZERO

        touch = levels[0].price
        crosses = (
            intent.limit_price >= touch
            if intent.side is OrderSide.BUY
            else intent.limit_price <= touch
        )
        if not crosses:
            return intent.size_shares if RESTING_ORDERS_FILL else ZERO

        available = ZERO
        for level in levels:
            # Inclusive of the limit: a resting ask at exactly our limit price is
            # takeable by our order.
            if intent.side is OrderSide.BUY and level.price > intent.limit_price:
                break
            if intent.side is OrderSide.SELL and level.price < intent.limit_price:
                break
            available += level.size
            if available >= intent.size_shares:
                return intent.size_shares
        return min(available, intent.size_shares)

    def _fee(self, token_id: ClobTokenId, *, shares: Decimal, price: Decimal) -> Decimal:
        """Taker fee the live path would have charged for this fill.

        Zero when no schedule was observed -- and that is a gap in the simulation
        rather than a free trade, which is why :meth:`observe` takes the schedule and
        why this is logged with every fill.
        """
        schedule = self._fees.get(token_id)
        if schedule is None:
            return ZERO
        return venue.taker_fee(
            shares=shares, price=price, rate=schedule.rate, exponent=schedule.exponent
        )

    def _record(
        self,
        intent: OrderIntent,
        *,
        status: OrderStatus,
        filled_shares: Decimal = ZERO,
        average_fill_price: Decimal | None = None,
        error: str | None = None,
    ) -> OrderRecord:
        self._sequence += 1
        now = self._clock.now()
        return OrderRecord(
            client_key=intent.client_key,
            # Prefixed so a paper order id can never be mistaken for a venue one in
            # a log, a database row, or a reconciliation report.
            order_id=OrderId(f"paper-{self._sequence}"),
            status=status,
            filled_shares=filled_shares,
            average_fill_price=average_fill_price,
            submitted_at=now,
            updated_at=now,
            error=error,
        )


class ShadowExecutor(ModeExecutor):
    """Builds and validates a real order, then declines to send it.

    The last rehearsal before live. Everything a LIVE run would do happens --
    signing, formatting, venue-side validation of everything checkable without
    submitting -- and the transmission is suppressed. It catches the failures
    paper trading structurally cannot: malformed orders, tick-size violations,
    missing approvals, signature problems.

    What it validates here is the subset checkable **without credentials**: the
    price is on a tick grid, the size is positive and on its grid, the limit is
    inside the unit interval, and a BUY carries a collateral cap. Signing and
    venue-side validation need a key and are therefore part of the live adapter's
    own path, which this executor deliberately does not fake -- a shadow run that
    reported "signed successfully" without a key would be the opposite of a
    rehearsal.
    """

    mode = RunMode.SHADOW

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self.suppressed: list[OrderIntent] = []
        """Every order that would have been sent, in order. The record a shadow run
        exists to produce."""

    async def submit(self, intent: OrderIntent) -> OrderRecord:
        """Validate as far as possible without credentials, then suppress the send."""
        problems = self._validate(intent)
        now = self._clock.now()
        self.suppressed.append(intent)

        if problems:
            log.warning(
                "shadow.invalid_order",
                client_key=str(intent.client_key),
                problems=problems,
            )
            return OrderRecord(
                client_key=intent.client_key,
                order_id=None,
                status=OrderStatus.REJECTED,
                submitted_at=now,
                updated_at=now,
                error="; ".join(problems),
            )

        log.info(
            "shadow.suppressed",
            client_key=str(intent.client_key),
            size_shares=str(intent.size_shares),
            limit_price=str(intent.limit_price),
        )
        return OrderRecord(
            client_key=intent.client_key,
            order_id=OrderId(f"shadow-{len(self.suppressed)}"),
            # Not FILLED: nothing was sent, so claiming a fill would put a fictional
            # position into the ledger. OPEN says "this would be working now".
            status=OrderStatus.OPEN,
            submitted_at=now,
            updated_at=now,
        )

    @staticmethod
    def _validate(intent: OrderIntent) -> list[str]:
        problems: list[str] = []
        if intent.size_shares <= 0:
            problems.append(f"non-positive size {intent.size_shares}")
        if not (Decimal(0) < intent.limit_price < Decimal(1)):
            problems.append(f"limit price {intent.limit_price} outside (0, 1)")
        if intent.side is OrderSide.BUY and intent.max_spend is None:
            problems.append("BUY without max_spend: a taker fee is charged on top")
        if intent.limit_price != intent.limit_price.quantize(SHADOW_MIN_TICK):
            # The finest grid the venue uses. A price failing this fails on every
            # market; one passing it may still violate a coarser market's tick, which
            # only the market's own tick_size can decide.
            problems.append(f"limit price {intent.limit_price} finer than {SHADOW_MIN_TICK}")
        return problems


#: Finest tick the venue quotes. See ``venue.TICK_SIZES``.
SHADOW_MIN_TICK: Final = Decimal("0.001")
