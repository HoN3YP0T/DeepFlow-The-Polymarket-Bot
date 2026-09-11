"""Market classification. Section 3.

Rule-based for V1: tag matching, then keyword/pattern matching over the
question and resolution text, then a confidence score.

The important property is not accuracy, it is calibration at the low end. A
classifier that confidently mislabels a cricket market as football routes it to
a model whose state variables are meaningless for it. Anything below the
configured confidence threshold becomes ``UNKNOWN``, which is never traded.
"""

from __future__ import annotations

from decimal import Decimal

from deepflow.config.thresholds import Thresholds
from deepflow.core.domain import Classification, Market
from deepflow.core.enums import MarketCategory
from deepflow.core.logging import get_logger

log = get_logger(__name__)

#: Tag/keyword seeds per category. Deliberately data, not code: the
#: geopolitical set in particular must grow without a release, and hardcoding
#: today's conflicts guarantees the classifier is stale by next quarter.
CATEGORY_SIGNALS: dict[MarketCategory, tuple[str, ...]] = {
    MarketCategory.FOOTBALL: ("football", "soccer", "premier league", "la liga", "uefa", "fifa"),
    MarketCategory.CRICKET: ("cricket", "odi", "t20", "test match", "ipl", "wicket"),
    MarketCategory.TENNIS: ("tennis", "atp", "wta", "wimbledon", "us open", "roland garros"),
    MarketCategory.BADMINTON: ("badminton", "bwf", "shuttlecock"),
    MarketCategory.BTC_5M: ("btc", "bitcoin"),
    MarketCategory.CRYPTO: ("crypto", "ethereum", "solana", "token"),
    MarketCategory.POLITICS: ("election", "president", "senate", "parliament", "nominee"),
    MarketCategory.GEOPOLITICS: ("sanctions", "treaty", "summit", "diplomatic"),
    MarketCategory.WAR_CONFLICT: ("strike", "invasion", "offensive", "military action"),
    MarketCategory.CEASEFIRE: ("ceasefire", "truce", "armistice", "peace deal"),
}


class MarketClassifier:
    """Assigns a category and a confidence to a discovered market."""

    def __init__(self, thresholds: Thresholds) -> None:
        self._thresholds = thresholds

    def classify(self, market: Market) -> Classification:
        """Classify ``market``.

        TODO(skeleton): score each category from tag hits and question/
        resolution-text matches, take the best, and demote to UNKNOWN when the
        winner is below ``classification_min_confidence`` or when the top two
        categories are too close to separate.

        BTC 5-minute markets need a dedicated check beyond the ``btc`` keyword:
        the distinguishing feature is a sub-hourly expiry against a reference
        price, which is what makes them a different modelling problem from a
        general crypto market rather than a faster one.
        """
        raise NotImplementedError("MarketClassifier.classify")

    @staticmethod
    def unknown(reason: str) -> Classification:
        """The safe fallback. Never auto-traded."""
        return Classification(
            category=MarketCategory.UNKNOWN,
            confidence=Decimal(0),
            rationale=reason,
        )
