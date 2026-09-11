"""Discovery loop. Section 3.

Polls for active markets, drives each one from DISCOVERED through CLASSIFIED to
VALIDATED, and hands the survivors to the monitoring set.

Runs on a slow cadence deliberately: the market catalogue changes on the order
of minutes, and re-validating resolution rules on every tick wastes calls that
the trading path needs.
"""

from __future__ import annotations

from deepflow.config.thresholds import Thresholds
from deepflow.core.logging import get_logger
from deepflow.core.state_machine import MarketLifecycle, MarketState
from deepflow.pipeline.classifier import MarketClassifier
from deepflow.pipeline.resolution import ResolutionValidator
from deepflow.ports.market_data import MarketDiscoveryPort

log = get_logger(__name__)


class DiscoveryService:
    """Finds, classifies and validates markets."""

    def __init__(
        self,
        *,
        discovery: MarketDiscoveryPort,
        classifier: MarketClassifier,
        validator: ResolutionValidator,
        thresholds: Thresholds,
    ) -> None:
        self._discovery = discovery
        self._classifier = classifier
        self._validator = validator
        self._thresholds = thresholds
        self._lifecycles: dict[str, MarketLifecycle] = {}

    async def run_once(self) -> None:
        """One discovery sweep.

        TODO(skeleton): for each active market --
        1. ``DISCOVERED`` -> classify -> ``CLASSIFIED``; UNKNOWN category goes
           to ``MARKET_INVALID`` and is not retried this sweep.
        2. ``CLASSIFIED`` -> validate resolution -> ``VALIDATED``; anything
           other than VALID goes to ``MARKET_INVALID``.
        3. ``VALIDATED`` -> ``MONITORED`` and join the streaming set.

        Both rejections are journalled with their reason. The rejected set is
        the evidence for whether the gates are calibrated.
        """
        raise NotImplementedError("DiscoveryService.run_once")

    def lifecycle_for(self, condition_id: str) -> MarketLifecycle:
        """Lifecycle holder for a market, created on first sight."""
        return self._lifecycles.setdefault(condition_id, MarketLifecycle())

    @property
    def monitored_count(self) -> int:
        return sum(
            1 for lc in self._lifecycles.values() if lc.state is not MarketState.MARKET_INVALID
        )
