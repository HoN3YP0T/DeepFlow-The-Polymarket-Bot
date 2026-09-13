"""Geopolitical probability engine. Sections 13-14."""

from __future__ import annotations

from deepflow.core.enums import MarketCategory
from deepflow.engines.event_driven import EventDrivenEngine


class GeopoliticalEngine(EventDrivenEngine):
    """Probability for conflict, ceasefire and diplomatic markets.

    Conflict categories are classified dynamically. The brief lists current theatres as
    examples; hardcoding them would leave the system blind to the next one, which is
    precisely when these markets are most mispriced.

    Carries the full :data:`~deepflow.engines.event_driven.MAX_EVENT_SHIFT`: a conflict
    market's situation genuinely unfolds, so an event short of resolution still moves the
    true probability. The reasoning itself lives in
    :class:`~deepflow.engines.event_driven.EventDrivenEngine`.
    """

    name = "geopolitical"
    categories = frozenset(
        {
            MarketCategory.GEOPOLITICS,
            MarketCategory.WAR_CONFLICT,
            MarketCategory.CEASEFIRE,
            MarketCategory.MILITARY_DIPLOMATIC,
        }
    )
