"""Engine registry.

Maps a classification to the engine that owns it. Explicit registration rather
than import-time magic: a market must never silently fall through to a generic
model because no engine matched.
"""

from __future__ import annotations

from deepflow.core.domain import Classification
from deepflow.core.enums import MarketCategory
from deepflow.core.logging import get_logger
from deepflow.ports.probability import ProbabilityEnginePort

log = get_logger(__name__)


class EngineRegistry:
    """Category -> engine lookup."""

    def __init__(self) -> None:
        self._by_category: dict[MarketCategory, ProbabilityEnginePort] = {}

    def register(self, engine: ProbabilityEnginePort) -> None:
        """Register ``engine`` for each category it claims.

        Raises on a duplicate claim. Two engines competing for one category is
        a wiring bug, and resolving it by last-write-wins would mean the model
        that runs depends on import order.
        """
        for category in engine.categories:
            if category in self._by_category:
                raise ValueError(
                    f"category {category} already handled by {self._by_category[category].name}"
                )
            self._by_category[category] = engine

    def resolve(self, classification: Classification) -> ProbabilityEnginePort | None:
        """Engine for this classification, or ``None``.

        ``None`` means no trade. There is deliberately no fallback engine: a
        generic model applied to a sport it does not understand is worse than
        no model, because it still produces a confident-looking number.
        """
        if not classification.is_tradeable:
            return None
        return self._by_category.get(classification.category)

    @property
    def registered_categories(self) -> frozenset[MarketCategory]:
        return frozenset(self._by_category)

    @property
    def engines(self) -> tuple[ProbabilityEnginePort, ...]:
        """Each registered engine once, in registration order.

        De-duplicated because one engine may claim several categories, and anything
        applied per-engine -- installing a calibration curve, most obviously -- must
        happen once rather than once per category it answers for.
        """
        seen: dict[int, ProbabilityEnginePort] = {}
        for engine in self._by_category.values():
            seen.setdefault(id(engine), engine)
        return tuple(seen.values())
