"""Cross-market relationship engine. Section 15.

Detects related markets, mutually exclusive outcomes, logical dependencies and
probability inconsistencies -- for example a set of exhaustive outcomes whose
prices sum to more than one.

Disabled until backtested, and the flag is explicit
(``CrossMarketThresholds.backtest_validated``). Apparent arbitrage between
prediction markets is usually not arbitrage: the two markets resolve on
different sources, different deadlines, or different definitions of the same
event, and the "free" spread is the price of that difference. Capital gets lost
here while the position looks risk-free on a dashboard.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from deepflow.config.thresholds import CrossMarketThresholds
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId

log = get_logger(__name__)


class RelationKind(StrEnum):
    MUTUALLY_EXCLUSIVE = "MUTUALLY_EXCLUSIVE"
    EXHAUSTIVE = "EXHAUSTIVE"
    IMPLICATION = "IMPLICATION"
    """A implies B: P(A) may never exceed P(B)."""
    CONDITIONAL = "CONDITIONAL"
    CORRELATED = "CORRELATED"


class MarketRelation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RelationKind
    members: tuple[ConditionId, ...]
    confidence: Decimal
    rationale: str
    resolution_sources_match: bool = False
    """False means the members resolve against different sources or deadlines.
    Any inconsistency between them is then explainable without a mispricing,
    and must not be traded as one."""


class CrossMarketEngine:
    """Finds and evaluates relationships between markets."""

    def __init__(self, thresholds: CrossMarketThresholds) -> None:
        self._thresholds = thresholds

    async def find_relations(
        self, condition_ids: Sequence[ConditionId]
    ) -> Sequence[MarketRelation]:
        """TODO(skeleton): group by event, compare parsed resolution criteria,
        and infer relation kinds. Entity overlap in a title is not a relation."""
        raise NotImplementedError("CrossMarketEngine.find_relations")

    async def find_inconsistencies(
        self, relation: MarketRelation
    ) -> Sequence[object]:
        """Probability violations implied by a relation.

        Reported for observability whatever the flags say; only tradeable once
        ``backtest_validated`` is set *and* the members share a resolution
        source and deadline.
        """
        raise NotImplementedError("CrossMarketEngine.find_inconsistencies")

    @property
    def is_tradeable(self) -> bool:
        return self._thresholds.enabled and self._thresholds.backtest_validated
