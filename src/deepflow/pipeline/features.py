"""Feature engine. Section 5.

Folds raw book/trade/stream updates into the derived quantities the models and
gates consume, and -- equally important -- decides whether the resulting
snapshot is fit to trade on.

Freshness is computed per snapshot rather than held as a global flag, because
"the WebSocket is up" and "this particular market's state is current" are
different claims. A market with no updates for two minutes is stale even on a
perfectly healthy connection.
"""

from __future__ import annotations

from datetime import datetime

from deepflow.config.thresholds import Thresholds
from deepflow.core.clock import Clock
from deepflow.core.domain import MarketSnapshot, Microstructure, OrderBook, PublicTrade
from deepflow.core.enums import DataQuality
from deepflow.core.logging import get_logger
from deepflow.core.types import ONE, ZERO

log = get_logger(__name__)

#: Clock disagreement tolerated before a snapshot is called inconsistent.
#: Venue and local clocks drift, and timestamps are assigned server-side before
#: the frame reaches us, so a fraction of a second ahead is normal rather than
#: alarming. Beyond that, something is wrong with a clock and the freshness check
#: is no longer protecting anything.
CLOCK_SKEW_TOLERANCE_SECONDS = 2.0

#: Severity ranking, for taking the worst verdict across several books.
#: INCONSISTENT outranks STALE deliberately: stale data was true at a past
#: moment, whereas inconsistent data was never true at all.
SEVERITY: dict[DataQuality, int] = {
    DataQuality.FRESH: 0,
    DataQuality.DEGRADED: 1,
    DataQuality.STALE: 2,
    DataQuality.INCONSISTENT: 3,
}


class FeatureEngine:
    """Computes microstructure features and data-quality verdicts."""

    def __init__(self, thresholds: Thresholds, clock: Clock) -> None:
        self._thresholds = thresholds
        self._clock = clock

    def compute(
        self,
        *,
        book: OrderBook,
        trades: tuple[PublicTrade, ...],
        history: tuple[OrderBook, ...] = (),
    ) -> Microstructure:
        """Derive order-flow features.

        TODO(skeleton):
        * book imbalance -- size-weighted, depth-limited. Top-of-book alone is
          trivially spoofable on a thin market.
        * flow imbalance -- signed traded volume over a rolling window.
        * liquidity concentration -- how much depth sits at one level; a book
          that is one large order deep is not liquid, it is one cancellation
          from empty.
        * slippage and impact -- walk the book for the intended size.
        * velocity/acceleration -- from ``history``.
        * abnormal move -- flag when the move is large relative to the market's
          own recent volatility, not against a fixed threshold.

        Every field stays ``None`` when its inputs are insufficient. A zero
        would be read downstream as a real, balanced measurement.
        """
        raise NotImplementedError("FeatureEngine.compute")

    def assess_quality(
        self,
        *,
        captured_at: datetime,
        book: OrderBook,
        max_age_seconds: float | None = None,
        has_gap: bool = False,
    ) -> DataQuality:
        """Classify snapshot freshness and internal consistency.

        Returns STALE past the age budget, INCONSISTENT for a book that cannot be
        true, DEGRADED in the grey zone approaching the budget or after a stream
        gap, else FRESH. Only FRESH permits a new entry; DEGRADED still allows
        exits, because refusing to act on an open position is the worse failure.

        Consistency is checked *before* age, because an inconsistent book is wrong
        rather than merely old, and reporting it as stale would send the caller
        looking for a latency problem that does not exist.

        ``has_gap`` comes from the stream adapter: a book folded across a
        reconnect may be missing the update that moved it. Such a book can never
        be FRESH however recent its timestamp, which is the entire point of
        tracking the gap -- a resumed stream produces state that looks current and
        is not.
        """
        budget = max_age_seconds or self._thresholds.breakers.max_data_age_seconds

        inconsistency = self._inconsistency(book)
        if inconsistency is not None:
            log.warning(
                "features.book_inconsistent",
                token_id=book.token_id,
                reason=inconsistency,
            )
            return DataQuality.INCONSISTENT

        age_seconds = (self._clock.now() - captured_at).total_seconds()

        # A snapshot from the future means a clock disagreement, ours or theirs.
        # It must not read as fresh: a negative age passes every freshness
        # comparison trivially, so the one thing a skewed clock guarantees is that
        # the check protecting us stops working.
        if age_seconds < -CLOCK_SKEW_TOLERANCE_SECONDS:
            log.warning(
                "features.timestamp_in_future",
                token_id=book.token_id,
                ahead_by=round(-age_seconds, 3),
            )
            return DataQuality.INCONSISTENT

        if age_seconds > budget:
            return DataQuality.STALE

        if has_gap:
            return DataQuality.DEGRADED

        degraded_after = budget * float(self._thresholds.breakers.degraded_age_fraction)
        if age_seconds > degraded_after:
            return DataQuality.DEGRADED

        return DataQuality.FRESH

    def assess_snapshot(
        self, snapshot: MarketSnapshot, *, max_age_seconds: float | None = None
    ) -> DataQuality:
        """Re-assess a whole snapshot, taking the worst verdict across its books.

        Worst-of, not average: a market whose YES book is current and whose NO
        book is two minutes old is not half-fresh. Pricing either side needs both,
        because the complement is what tells us the two agree.

        The snapshot's own quality is folded in rather than overwritten. The stream
        adapter may already have marked it DEGRADED for a reason this method cannot
        see -- a dropped event, a reconnect -- and re-deriving quality from
        timestamps alone would launder that away.
        """
        verdicts = [snapshot.quality]
        verdicts.extend(
            self.assess_quality(
                captured_at=book.captured_at, book=book, max_age_seconds=max_age_seconds
            )
            for book in snapshot.books
        )
        if not snapshot.books:
            # No books is not a fresh market with no depth; it is the absence of
            # any observation at all.
            return DataQuality.INCONSISTENT
        return max(verdicts, key=SEVERITY.__getitem__)

    @staticmethod
    def _inconsistency(book: OrderBook) -> str | None:
        """Why this book cannot be true, or ``None`` if it can.

        A crossed book is unreachable here -- :class:`OrderBook` rejects one at
        construction -- and is checked anyway as defence in depth, because this is
        the last gate before a price reaches a model and the cost of the check is
        one comparison.
        """
        if not book.bids and not book.asks:
            return "empty book"

        best_bid, best_ask = book.best_bid, book.best_ask
        if best_bid is not None and best_ask is not None and best_bid >= best_ask:
            return "crossed book"

        for side, levels in (("bid", book.bids), ("ask", book.asks)):
            for level in levels:
                if level.size <= 0:
                    return f"non-positive size on {side} at {level.price}"
                # Outcome tokens settle at 0 or 1, so a resting price outside the
                # open interval is not a wide quote -- it is a corrupt level, and
                # walking the book through it would produce a fill estimate that
                # cannot happen.
                if not ZERO < level.price < ONE:
                    return f"{side} price {level.price} outside (0, 1)"

        return None
