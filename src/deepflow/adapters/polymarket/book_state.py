"""Incremental order-book state, folded from stream events.

Pure and synchronous on purpose. Folding a book is the part of the stream
adapter that is easy to get subtly wrong and hard to notice, so it is separated
from the socket plumbing and tested on its own.

Semantics verified against the live feed on 2026-09-12 (420 level changes across
30 books, then compared against fresh REST snapshots -- 8 of 8 agreed at the
touch):

* ``book`` is a **full snapshot**. It replaces state entirely.
* ``price_change`` carries one or more level changes, each with its own
  ``asset_id``, so a single event can update both sides of a market.
* A change's ``size`` is the level's **new total**, not a delta. Zero removes the
  level. Adding sizes instead would inflate depth without bound, and every
  slippage and liquidity figure derived from the book would grow with it -- in the
  direction that makes trades look *safer*.
* A change's ``price`` is **not necessarily the touch**. A 20,000-share bid at
  0.10 on a market trading at 0.92 arrives as a ``price_change`` at 0.10. Reading
  it as the new best price would look like a 82-cent crash.
* Each change also reports ``best_bid`` / ``best_ask``, which *is* the touch. That
  gives a free consistency check: fold the level, then compare our computed touch
  against the reported one. Disagreement means our book has drifted and must be
  re-anchored from REST -- see :meth:`BookState.drifted`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from deepflow.core.domain import BookLevel, OrderBook
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId

log = get_logger(__name__)

BUY = "BUY"
SELL = "SELL"


@dataclass(slots=True)
class BookState:
    """Mutable book for one outcome token.

    Levels are held as ``price -> size`` maps rather than sorted sequences. A
    level change is a point update, so a dict makes it O(1) and removes any
    chance of an insertion landing in the wrong position; the sort happens once,
    on the way out, in :meth:`snapshot`.
    """

    token_id: ClobTokenId
    bids: dict[Decimal, Decimal] = field(default_factory=dict)
    asks: dict[Decimal, Decimal] = field(default_factory=dict)
    updated_at: datetime | None = None
    tick_size: Decimal | None = None

    #: Touch as the venue last reported it, for cross-checking our own fold.
    reported_best_bid: Decimal | None = None
    reported_best_ask: Decimal | None = None

    #: Set when a reconnect may have dropped updates. Cleared only by a fresh
    #: snapshot, never by a subsequent incremental change -- an incremental update
    #: on top of a book with a hole in it is still a book with a hole in it.
    has_gap: bool = False

    def apply_snapshot(
        self,
        *,
        bids: list[tuple[Decimal, Decimal]],
        asks: list[tuple[Decimal, Decimal]],
        timestamp: datetime,
        tick_size: Decimal | None = None,
    ) -> None:
        """Replace the book wholesale from a ``book`` event.

        Clears the gap flag: a snapshot is ground truth, which is precisely why
        re-anchoring after a reconnect means fetching one.
        """
        self.bids = {price: size for price, size in bids if size > 0}
        self.asks = {price: size for price, size in asks if size > 0}
        self.updated_at = timestamp
        if tick_size is not None:
            self.tick_size = tick_size
        self.has_gap = False

    def apply_level(
        self,
        *,
        side: str,
        price: Decimal,
        size: Decimal,
        timestamp: datetime,
        best_bid: Decimal | None = None,
        best_ask: Decimal | None = None,
    ) -> None:
        """Apply one level change. ``size`` is the level's new total; 0 removes it."""
        levels = self.bids if side.upper() == BUY else self.asks
        if size <= 0:
            levels.pop(price, None)
        else:
            levels[price] = size

        self.updated_at = timestamp
        if best_bid is not None:
            self.reported_best_bid = best_bid
        if best_ask is not None:
            self.reported_best_ask = best_ask

    def mark_gap(self) -> None:
        """Record that updates may have been missed."""
        self.has_gap = True

    @property
    def best_bid(self) -> Decimal | None:
        return max(self.bids) if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return min(self.asks) if self.asks else None

    def drifted(self) -> bool:
        """Whether our folded touch disagrees with the venue's reported touch.

        The cheapest possible integrity check, and it costs nothing: the venue
        sends the touch alongside every level change, so a mismatch is detectable
        immediately rather than at the next REST poll. Any disagreement means a
        dropped or misapplied update, and the book must be re-anchored -- trading
        on a book that has silently diverged is the failure this guards.

        Absent reported values read as "nothing to compare", not as agreement.
        """
        if self.reported_best_bid is not None and self.best_bid != self.reported_best_bid:
            return True
        return self.reported_best_ask is not None and self.best_ask != self.reported_best_ask

    def is_empty(self) -> bool:
        return not self.bids and not self.asks

    def snapshot(self) -> OrderBook | None:
        """Materialize an immutable :class:`OrderBook`, best price first.

        ``None`` when the book is empty or has not been timestamped -- there is no
        such thing as a snapshot of a book we have never seen, and returning an
        empty one would read downstream as a real, illiquid market.

        A crossed book is dropped rather than returned. Crossing means our state
        is wrong (a stale level that should have been removed), and
        :class:`OrderBook` rejects it anyway; catching it here turns a raised
        exception in the middle of the event pump into a logged, recoverable
        condition.
        """
        if self.updated_at is None or self.is_empty():
            return None

        bids = tuple(
            BookLevel(price=price, size=self.bids[price])
            for price in sorted(self.bids, reverse=True)
        )
        asks = tuple(BookLevel(price=price, size=self.asks[price]) for price in sorted(self.asks))

        if bids and asks and bids[0].price >= asks[0].price:
            log.warning(
                "book_state.crossed",
                token_id=self.token_id,
                best_bid=str(bids[0].price),
                best_ask=str(asks[0].price),
            )
            self.mark_gap()
            return None

        return OrderBook(token_id=self.token_id, bids=bids, asks=asks, captured_at=self.updated_at)
