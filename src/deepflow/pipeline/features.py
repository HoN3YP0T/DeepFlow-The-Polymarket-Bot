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
from decimal import Decimal
from itertools import pairwise

from deepflow.config.thresholds import Thresholds
from deepflow.core.clock import Clock
from deepflow.core.domain import (
    BookLevel,
    MarketSnapshot,
    Microstructure,
    OrderBook,
    PublicTrade,
)
from deepflow.core.enums import DataQuality, OrderSide
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


def _levels_within(
    levels: tuple[BookLevel, ...],
    touch: Decimal,
    band: Decimal,
    *,
    descending: bool,
) -> list[BookLevel]:
    """Levels within ``band`` of the touch. Books are best-price-first."""
    limit = touch - band if descending else touch + band
    return [
        level for level in levels if (level.price >= limit if descending else level.price <= limit)
    ]


def _depth_within(
    levels: tuple[BookLevel, ...],
    touch: Decimal | None,
    band: Decimal,
    *,
    descending: bool,
) -> Decimal:
    if touch is None:
        return Decimal(0)
    return sum(
        (level.size for level in _levels_within(levels, touch, band, descending=descending)),
        Decimal(0),
    )


class FeatureEngine:
    """Computes microstructure features and data-quality verdicts."""

    def __init__(self, thresholds: Thresholds, clock: Clock) -> None:
        self._thresholds = thresholds
        self._clock = clock

    def compute(
        self,
        *,
        book: OrderBook,
        trades: tuple[PublicTrade, ...] = (),
        history: tuple[OrderBook, ...] = (),
        size_shares: Decimal | None = None,
    ) -> Microstructure:
        """Derive order-flow features.

        Every field stays ``None`` when its inputs are insufficient. A zero would be
        read downstream as a real, balanced measurement, and the whole point of the
        optional fields is that "no information" is a different claim from "neutral".

        Depth is always measured inside a band around the touch, never across the
        whole book -- see :attr:`MicrostructureThresholds.depth_band` for why that is
        the definition rather than a parameter.
        """
        micro = self._thresholds.microstructure
        band = micro.depth_band

        bid_depth = _depth_within(book.bids, book.best_bid, band, descending=True)
        ask_depth = _depth_within(book.asks, book.best_ask, band, descending=False)

        return Microstructure(
            book_imbalance=self._book_imbalance(book, bid_depth, ask_depth),
            flow_imbalance=self._flow_imbalance(trades),
            liquidity_concentration=self._concentration(book, band),
            estimated_slippage_bps=self._slippage_bps(book, size_shares),
            price_velocity=self._velocity(history),
            price_acceleration=self._acceleration(history),
            abnormal_move=self._abnormal(history),
        )

    # --- Features ---------------------------------------------------------
    def _book_imbalance(
        self, book: OrderBook, bid_depth: Decimal, ask_depth: Decimal
    ) -> Decimal | None:
        """Signed depth imbalance inside the band, or ``None`` if not measurable.

        Returns ``None`` on three conditions, each of which would otherwise produce
        a confident-looking number from nothing:

        * a one-sided book -- there is no ratio to take
        * combined depth below ``min_depth_for_imbalance`` -- a ratio of two tiny
          numbers is noise wearing a signal's clothing
        * the sign disagreeing with the same measure at a wider band, which means
          the imbalance is an artifact of where the band was drawn rather than a
          property of the market. Observed live on real books: one market read
          -0.88 within a cent and +0.17 within five.
        """
        micro = self._thresholds.microstructure
        if book.best_bid is None or book.best_ask is None:
            return None

        total = bid_depth + ask_depth
        if total < micro.min_depth_for_imbalance:
            return None

        imbalance = (bid_depth - ask_depth) / total

        wide = micro.depth_band * micro.confirm_band_multiple
        wide_bid = _depth_within(book.bids, book.best_bid, wide, descending=True)
        wide_ask = _depth_within(book.asks, book.best_ask, wide, descending=False)
        wide_total = wide_bid + wide_ask
        if wide_total > 0:
            wide_imbalance = (wide_bid - wide_ask) / wide_total
            if imbalance * wide_imbalance < 0:
                log.info(
                    "features.imbalance_not_robust",
                    token_id=book.token_id,
                    narrow=str(round(imbalance, 3)),
                    wide=str(round(wide_imbalance, 3)),
                )
                return None

        return imbalance

    @staticmethod
    def _flow_imbalance(trades: tuple[PublicTrade, ...]) -> Decimal | None:
        """Signed traded volume: (bought - sold) / total, over the window given.

        The caller owns the window. Passing a longer history here is a different
        measurement, not a better one, and hiding the choice inside this method
        would make two callers' numbers incomparable.
        """
        if not trades:
            return None
        bought = sum((t.size for t in trades if t.side is OrderSide.BUY), Decimal(0))
        total = sum((t.size for t in trades), Decimal(0))
        if total <= 0:
            return None
        return (bought - (total - bought)) / total

    def _concentration(self, book: OrderBook, band: Decimal) -> Decimal | None:
        """Largest single level as a fraction of banded depth on the same side.

        A book that is one large order deep is not liquid; it is one cancellation
        from empty. Takes the worse (more concentrated) of the two sides, because a
        trade needs the side it crosses and an average would hide a hollow one.
        """
        worst: Decimal | None = None
        for levels, touch, descending in (
            (book.bids, book.best_bid, True),
            (book.asks, book.best_ask, False),
        ):
            if touch is None:
                continue
            inside = _levels_within(levels, touch, band, descending=descending)
            total = sum((level.size for level in inside), Decimal(0))
            if total <= 0:
                continue
            share = max(level.size for level in inside) / total
            worst = share if worst is None else max(worst, share)
        return worst

    @staticmethod
    def _slippage_bps(book: OrderBook, size_shares: Decimal | None) -> Decimal | None:
        """Cost of sweeping ``size_shares`` of asks, in bps over the touch.

        ``None`` when the book cannot fill the size at all -- distinct from a poor
        price. A partial walk reported as a slippage estimate would understate the
        cost precisely when the book is too thin to trade.
        """
        if size_shares is None or size_shares <= 0 or book.best_ask is None:
            return None

        vwap = book.vwap_to_fill(size_shares, side=OrderSide.BUY)
        if vwap is None:
            return None
        return (vwap - book.best_ask) / book.best_ask * Decimal(10_000)

    def _velocity(self, history: tuple[OrderBook, ...]) -> Decimal | None:
        """Mid-price change per second across the history window."""
        if len(history) < self._thresholds.microstructure.min_history_for_velocity:
            return None
        points = [(b.captured_at, b.mid) for b in history if b.mid is not None]
        if len(points) < 2:
            return None
        seconds = Decimal(str((points[-1][0] - points[0][0]).total_seconds()))
        if seconds <= 0:
            return None
        first, last = points[0][1], points[-1][1]
        assert first is not None and last is not None
        return (last - first) / seconds

    def _acceleration(self, history: tuple[OrderBook, ...]) -> Decimal | None:
        """Change in velocity between the two halves of the window.

        Needs twice the history velocity needs: a single slope cannot accelerate.
        """
        minimum = self._thresholds.microstructure.min_history_for_velocity * 2
        if len(history) < minimum:
            return None
        midpoint = len(history) // 2
        early = self._velocity(history[:midpoint])
        late = self._velocity(history[midpoint:])
        if early is None or late is None:
            return None
        return late - early

    def _abnormal(self, history: tuple[OrderBook, ...]) -> bool:
        """Whether the latest move is large against the market's *own* volatility.

        Relative, not absolute: a 2c move is nothing on a 0.50 market and enormous
        on a 0.02 one, and a fixed threshold would either ignore the second or flag
        the first constantly.
        """
        micro = self._thresholds.microstructure
        mids = [b.mid for b in history if b.mid is not None]
        if len(mids) < micro.min_history_for_velocity + 1:
            return False

        steps = [abs(b - a) for a, b in pairwise(mids)]
        if len(steps) < 2:
            return False
        latest, prior = steps[-1], steps[:-1]
        mean = sum(prior, Decimal(0)) / len(prior)
        if mean <= 0:
            # No prior movement at all: any move is a change in regime, but calling
            # the first tick after a flat book "abnormal" would fire on every market
            # that has simply been quiet.
            return False
        return latest > mean * micro.abnormal_move_sigma

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
