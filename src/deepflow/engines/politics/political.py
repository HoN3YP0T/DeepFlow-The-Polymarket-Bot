"""Political event model. Section 13.

Elections, appointments, court and policy decisions. Unlike sports, there is no
continuously observable state that moves the true probability -- it updates in
discrete jumps on announcements, and sits still in between.

The consequence for this system: most of the time the honest estimate is
"the market is probably right", and the engine abstains. It has an opinion only
when it holds evidence the market has not priced, which in practice means a
verified event from the geopolitical event pipeline. An engine that always
produces a number here would be manufacturing edge out of nothing.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Final

from deepflow.config.thresholds import GeopoliticsThresholds
from deepflow.core.domain import BaseRate
from deepflow.core.enums import MarketCategory
from deepflow.core.types import ConditionId
from deepflow.engines.event_driven import EventDrivenEngine
from deepflow.engines.geopolitics.events import EventPipeline, GeopoliticalEvent

#: How much of a conflict market's event shift a political market gets.
#:
#: Half. Between scheduled announcements a political market's true probability does not
#: drift, so a news item short of the announcement itself carries less information than
#: the same item in an unfolding conflict.
POLITICAL_SHIFT_SCALE: Final = Decimal("0.5")


class PoliticalEngine(EventDrivenEngine):
    """Probability for political and policy markets.

    Same mechanism as the geopolitical engine -- a sourced prior moved by corroborated,
    unpriced events -- and both take it from
    :class:`~deepflow.engines.event_driven.EventDrivenEngine` rather than one inheriting
    from the other: a political market is not a kind of conflict market, and the shared
    part is the reasoning, not the subject.

    The cap is **tighter** here. A political market's resolution is usually a scheduled,
    discrete announcement rather than an unfolding situation: between announcements the
    true probability does not drift, so a news item that is not itself the announcement
    says less than the equivalent item in a conflict market. An election result *is* the
    resolution, and a market still trading after one is a market whose resolution is
    disputed -- which is a case for abstaining, not for a large shift.
    """

    name = "political"
    categories = frozenset({MarketCategory.POLITICS})

    def __init__(
        self,
        pipeline: EventPipeline,
        thresholds: GeopoliticsThresholds | None = None,
        *,
        base_rates: Mapping[ConditionId, BaseRate] | None = None,
    ) -> None:
        super().__init__(pipeline, thresholds or GeopoliticsThresholds(), base_rates=base_rates)

    def _shift(self, events: Sequence[GeopoliticalEvent]) -> Decimal:
        """As the parent, scaled down by :data:`POLITICAL_SHIFT_SCALE`.

        Deriving the scale from the parent's cap rather than restating a number keeps the
        two engines' relationship explicit: whatever the conflict cap becomes, this stays
        a defined fraction of it.
        """
        return super()._shift(events) * POLITICAL_SHIFT_SCALE
