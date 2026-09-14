"""Probability engine base class."""

from __future__ import annotations

from abc import ABC, abstractmethod
from decimal import Decimal

from deepflow.core.domain import (
    Classification,
    Market,
    MarketSnapshot,
    ProbabilityEstimate,
)
from deepflow.core.enums import MarketCategory
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId
from deepflow.engines.calibration import Calibrator, IdentityCalibrator

log = get_logger(__name__)


class BaseProbabilityEngine(ABC):
    """Common scaffolding for probability engines.

    Two contracts every subclass must honour:

    1. **Independence.** Derive the probability from state -- score, clock,
       strike distance, evidence -- never from the market price. An engine that
       anchors on the price produces an edge that is an artefact of its own
       input, and it will look most confident exactly when it is least useful.

    2. **Abstention.** Return ``None`` when the state needed is missing or
       inconsistent. Abstaining costs one skipped trade; inventing a number
       puts a fabricated probability into sizing.
    """

    name: str = "base"
    categories: frozenset[MarketCategory] = frozenset()

    #: Set by :meth:`use_calibrator`. Identity until a fit exists, so an
    #: uncalibrated engine is obvious rather than quietly wrong.
    _calibrator: Calibrator = IdentityCalibrator()

    def use_calibrator(self, calibrator: Calibrator) -> None:
        """Install a fitted curve for this engine.

        Injected rather than loaded here: an engine should not know about a
        database, and which fit is live is an operator's decision recorded in
        ``calibration_fits``, not something a constructor should go looking for.
        """
        self._calibrator = calibrator
        log.info(
            "engine.calibrator_installed",
            engine=self.name,
            calibrator=type(calibrator).__name__,
        )

    def supports(self, classification: Classification) -> bool:
        return classification.is_tradeable and classification.category in self.categories

    @abstractmethod
    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """Estimate P(outcome) for ``token_id``, or ``None`` to abstain."""

    def _calibrate(self, raw: Decimal) -> Decimal:
        """Map a raw model output onto a calibrated probability.

        Matters most in the 0.85-0.98 band this system targets: a model that says
        0.97 and is right 0.93 of the time turns a positive edge negative, and the
        error is invisible in a win-rate summary -- 93 right out of 100 reads as a
        triumph.

        Identity until :meth:`use_calibrator` installs a fit, and identity again
        wherever a fit has no evidence. An isotonic curve says nothing about model
        outputs it never saw, and the alternatives to identity there are worse:
        extrapolating invents a correction from nothing, and clipping to the nearest
        fitted value makes a confident claim out of missing data. See
        :mod:`deepflow.engines.calibration`.
        """
        return self._calibrator.apply(raw)

    def _calibration_is_supported(self, raw: Decimal) -> bool:
        """Whether the installed fit has evidence at ``raw``.

        Exposed so an engine can widen its uncertainty when it is estimating
        somewhere the curve cannot vouch for, rather than reporting a calibrated
        number that is silently just the raw one.
        """
        return self._calibrator.supports(raw)
