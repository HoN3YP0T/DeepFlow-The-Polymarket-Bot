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
from deepflow.core.domain import Microstructure, OrderBook, PublicTrade
from deepflow.core.enums import DataQuality
from deepflow.core.logging import get_logger

log = get_logger(__name__)


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
    ) -> DataQuality:
        """Classify snapshot freshness and internal consistency.

        Returns STALE past the age budget, INCONSISTENT for a crossed or empty
        book, DEGRADED in the grey zone approaching the budget, else FRESH.
        Only FRESH permits a new entry; DEGRADED still allows exits, because
        refusing to act on an open position is the worse failure.
        """
        raise NotImplementedError("FeatureEngine.assess_quality")
