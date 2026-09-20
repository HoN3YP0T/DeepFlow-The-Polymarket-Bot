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
        self._disabled: set[str] = set()

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
        engine = self._by_category.get(classification.category)
        if engine is not None and engine.name in self._disabled:
            # A disabled engine abstains exactly as an absent one does, so the market
            # falls out of the decision context rather than being priced and then
            # refused later. Turning a strategy off must stop it forming an opinion,
            # not merely stop it acting on one -- otherwise the journal fills with
            # decisions about a strategy nobody is running.
            return None
        return engine

    def set_enabled(self, name: str, enabled: bool) -> bool:
        """Turn one engine on or off by name. Returns ``False`` if no such engine.

        Runtime rather than configuration because the alternative during an incident is
        a restart, which costs the feed, the TWAP warm-up and every subscription -- a
        high price for silencing one misbehaving model.

        **Disabling is not a safety mechanism and must not be used as one.** It stops an
        engine forming opinions; it does nothing about a position already open, and it
        is not a halt. Pausing entries is the halt, and it is a different control.
        """
        if name not in {engine.name for engine in self.engines}:
            return False
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)
        log.warning("engine.toggled", engine=name, enabled=enabled)
        return True

    def is_enabled(self, name: str) -> bool:
        return name not in self._disabled

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
