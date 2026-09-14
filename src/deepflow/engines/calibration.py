"""Calibration: turning what a model said into what it is worth believing.

Roadmap item 15, and the last gap in Phase 3. A model that says 0.97 and is right
0.93 of the time turns a positive edge negative, and the error is invisible in a
win-rate summary -- 93 of 100 correct reads like a triumph. The band this system
targets, 0.85-0.98, is exactly where that arithmetic bites hardest, because the
whole edge is a few points wide.

**Why this was not merely unstarted.** ``_calibrate`` returned its input unchanged
for the life of this repo, and the reason turned out not to be missing code: nothing
in the schema had ever recorded how a market resolved. Only half of each
(prediction, outcome) pair was being stored, so no amount of running would have
produced a fit. The recording half lives in
:mod:`deepflow.pipeline.settlement`; the arithmetic is here.

**Isotonic, not Platt.** Platt scaling fits a sigmoid, which assumes the model's
error has one smooth shape across the whole probability range. The errors this
system expects are band-specific -- a barrier model whose volatility input is
biased low is overconfident at the extremes and roughly right in the middle -- and a
sigmoid cannot represent that without distorting the region it was right about.
Isotonic regression assumes only monotonicity: that a higher model output really
does mean a higher chance. Give up that assumption and there is nothing left worth
calibrating.

Implemented directly rather than pulled from scikit-learn. Pool-adjacent-violators
is thirty lines, this codebase does probability in ``Decimal`` and would have to
round-trip through float to use an array library, and the dependency would be
carried into every deployment for one offline fit.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Protocol, runtime_checkable

from deepflow.core.domain import CalibrationSample
from deepflow.core.logging import get_logger

log = get_logger(__name__)

#: Fewest samples that may produce a fit at all.
#:
#: A calibration curve is a claim about a model's bias, and a claim like that drawn
#: from a handful of settled markets is noise given a shape. The number is a floor
#: rather than a sufficiency test -- :data:`MIN_MARKETS` is the binding constraint
#: in practice, for the reason given there.
MIN_SAMPLES: Final = 200

#: Fewest **distinct markets** those samples must come from.
#:
#: This is the constraint that matters, and it is the one an unwary fit gets wrong.
#: Predictions on one market are not independent observations: a 5-minute crypto
#: window sampled every minute yields five rows that share a single coin flip, and a
#: political market sampled for a week yields hundreds that share one election. Count
#: rows and 300 samples can mean three outcomes; the curve then looks beautifully
#: tight around a result that had three chances to be wrong.
#:
#: 50 is not a statistical guarantee. It is the point below which the honest answer
#: is "not yet" rather than a number.
MIN_MARKETS: Final = 50

#: Width of a reliability bin, for the diagnostic table.
BIN_WIDTH: Final = Decimal("0.05")

_ZERO: Final = Decimal(0)
_ONE: Final = Decimal(1)


def _clamp(value: Decimal) -> Decimal:
    return min(_ONE, max(_ZERO, value))


@runtime_checkable
class Calibrator(Protocol):
    """Maps a raw model output onto a calibrated probability."""

    def apply(self, raw: Decimal) -> Decimal:
        """The calibrated probability for ``raw``."""
        ...

    def supports(self, raw: Decimal) -> bool:
        """Whether the fit has evidence at ``raw``, rather than an extrapolation."""
        ...


class IdentityCalibrator:
    """No adjustment, and honest about it.

    The default for every engine. An uncalibrated engine should be obvious rather
    than quietly wrong, and identity is the only mapping that adds no claim.
    """

    def apply(self, raw: Decimal) -> Decimal:
        return _clamp(raw)

    def supports(self, raw: Decimal) -> bool:
        return False


@dataclass(frozen=True)
class Knot:
    """One point on the fitted curve: a model output and what it was worth."""

    predicted: Decimal
    calibrated: Decimal
    weight: int
    """How many samples pooled into this knot. Carried so a reader can see which
    parts of the curve are evidence and which are one market's opinion."""


@dataclass(frozen=True)
class ReliabilityBin:
    """One row of the diagnostic table: what was promised against what happened."""

    lower: Decimal
    upper: Decimal
    count: int
    markets: int
    mean_predicted: Decimal
    mean_realized: Decimal

    @property
    def gap(self) -> Decimal:
        """Promised minus delivered. Positive is overconfidence."""
        return self.mean_predicted - self.mean_realized


@dataclass(frozen=True)
class CalibrationReport:
    """What a fit is worth, in the terms that decide whether to enable it.

    The headline output of Phase 8 is this table, not P&L: a curve that improves the
    Brier score while leaving a 6-point gap at 0.95 is a worse trade than the numbers
    suggest, because every trade this system wants to make lives at 0.95.
    """

    engine: str
    samples: int
    markets: int
    brier_before: Decimal
    brier_after: Decimal
    ece_before: Decimal
    ece_after: Decimal
    bins: tuple[ReliabilityBin, ...]

    @property
    def improves(self) -> bool:
        """Whether the fit beats leaving the model alone, on its own training data.

        A low bar deliberately: failing it means the fit is not merely unhelpful but
        actively worse than identity, which indicates a broken pipeline rather than a
        well-behaved model.
        """
        return self.brier_after <= self.brier_before


class IsotonicCalibrator:
    """A monotone step curve fitted by pool-adjacent-violators.

    Outside the range of model outputs the fit actually saw, :meth:`apply` returns
    its input unchanged. The alternatives are worse: extrapolating the curve invents
    a correction from no evidence, and clipping to the nearest fitted value makes a
    confident claim (0.97 is really 0.85) from the mere absence of data. Identity at
    least adds nothing. :meth:`supports` is how a caller tells the two regions apart,
    and :func:`fit` reports the covered range so that an operator enabling a fit can
    see whether it reaches the band they intend to trade.
    """

    def __init__(self, engine: str, knots: Sequence[Knot]) -> None:
        if not knots:
            raise ValueError("an isotonic calibrator needs at least one knot")
        self.engine = engine
        self.knots = tuple(knots)

    @property
    def lower(self) -> Decimal:
        return self.knots[0].predicted

    @property
    def upper(self) -> Decimal:
        return self.knots[-1].predicted

    def supports(self, raw: Decimal) -> bool:
        return self.lower <= raw <= self.upper

    def apply(self, raw: Decimal) -> Decimal:
        """Linear interpolation between knots; identity outside their range."""
        if not self.supports(raw):
            return _clamp(raw)
        previous = self.knots[0]
        if raw <= previous.predicted:
            return _clamp(previous.calibrated)
        for knot in self.knots[1:]:
            if raw <= knot.predicted:
                span = knot.predicted - previous.predicted
                if span <= 0:
                    return _clamp(knot.calibrated)
                ratio = (raw - previous.predicted) / span
                rise = knot.calibrated - previous.calibrated
                return _clamp(previous.calibrated + ratio * rise)
            previous = knot
        return _clamp(self.knots[-1].calibrated)

    def as_dict(self) -> dict[str, object]:
        """Serialisable form, for storing a fit beside the data that produced it."""
        return {
            "engine": self.engine,
            "knots": [
                {
                    "predicted": str(knot.predicted),
                    "calibrated": str(knot.calibrated),
                    "weight": knot.weight,
                }
                for knot in self.knots
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> IsotonicCalibrator:
        raw_knots = payload.get("knots")
        if not isinstance(raw_knots, list) or not raw_knots:
            raise ValueError("calibration payload carries no knots")
        knots = []
        for entry in raw_knots:
            if not isinstance(entry, dict):
                raise ValueError("calibration knot must be an object")
            knots.append(
                Knot(
                    predicted=Decimal(str(entry["predicted"])),
                    calibrated=Decimal(str(entry["calibrated"])),
                    weight=int(entry.get("weight", 0)),
                )
            )
        return cls(str(payload.get("engine", "")), knots)


def _pool_adjacent_violators(samples: Sequence[CalibrationSample]) -> list[Knot]:
    """The fit itself.

    Walk the samples in order of what the model predicted, keeping running blocks of
    (sum of outcomes, count). Whenever a block's mean falls below the one before it,
    the two are pooled -- that is the only operation, and repeating it to a fixed
    point yields the monotone curve closest to the data in squared error.
    """
    ordered = sorted(samples, key=lambda s: s.predicted)
    blocks: list[list[Decimal]] = []  # [sum_realized, count, sum_predicted]
    for sample in ordered:
        blocks.append([sample.realized, _ONE, sample.predicted])
        while len(blocks) > 1:
            last, previous = blocks[-1], blocks[-2]
            if previous[0] / previous[1] <= last[0] / last[1]:
                break
            blocks[-2] = [
                previous[0] + last[0],
                previous[1] + last[1],
                previous[2] + last[2],
            ]
            blocks.pop()
    return [
        Knot(
            # The knot sits at the mean prediction of its block rather than at a
            # block edge: a block spans a range of model outputs and its fitted
            # value describes that range as a whole, so anchoring at an edge would
            # shift the whole curve by half a block.
            predicted=block[2] / block[1],
            calibrated=block[0] / block[1],
            weight=int(block[1]),
        )
        for block in blocks
    ]


def brier_score(pairs: Iterable[tuple[Decimal, Decimal]]) -> Decimal:
    """Mean squared error between prediction and outcome.

    Proper: it is minimised only by reporting the true probability, so it cannot be
    gamed by a model that hedges toward 0.5 or one that shouts 0 and 1.
    """
    total = _ZERO
    count = 0
    for predicted, realized in pairs:
        total += (predicted - realized) ** 2
        count += 1
    return total / count if count else _ZERO


def expected_calibration_error(bins: Sequence[ReliabilityBin]) -> Decimal:
    """Sample-weighted mean absolute gap between promise and delivery.

    Reported alongside Brier because the two answer different questions. Brier mixes
    calibration with sharpness -- a model that says 0.5 to everything is perfectly
    calibrated and useless, and Brier correctly punishes it. ECE isolates the half
    that sizing depends on.
    """
    total = _ZERO
    count = 0
    for entry in bins:
        total += abs(entry.gap) * entry.count
        count += entry.count
    return total / count if count else _ZERO


def reliability_bins(
    samples: Sequence[CalibrationSample],
    *,
    calibrator: Calibrator | None = None,
    width: Decimal = BIN_WIDTH,
) -> tuple[ReliabilityBin, ...]:
    """Group samples by predicted probability and compare promise against outcome.

    ``markets`` is carried per bin, not just ``count``, because a bin holding 400
    samples drawn from 4 markets is four observations wearing a large number -- and
    the bins at the top of the range, which matter most here, are the ones most
    likely to be one market watched for a long time.
    """
    buckets: dict[int, list[CalibrationSample]] = {}
    for sample in samples:
        predicted = calibrator.apply(sample.predicted) if calibrator else sample.predicted
        index = min(int(predicted / width), int(_ONE / width) - 1)
        buckets.setdefault(index, []).append(sample.model_copy(update={"predicted": predicted}))
    rows = []
    for index in sorted(buckets):
        group = buckets[index]
        count = len(group)
        rows.append(
            ReliabilityBin(
                lower=width * index,
                upper=width * (index + 1),
                count=count,
                markets=len({s.condition_id for s in group}),
                mean_predicted=sum((s.predicted for s in group), _ZERO) / count,
                mean_realized=sum((s.realized for s in group), _ZERO) / count,
            )
        )
    return tuple(rows)


def fit(
    engine: str,
    samples: Sequence[CalibrationSample],
    *,
    min_samples: int = MIN_SAMPLES,
    min_markets: int = MIN_MARKETS,
) -> tuple[IsotonicCalibrator | None, CalibrationReport | None]:
    """Fit one engine's curve, or decline and say why.

    Returns ``(None, None)`` when there is not enough evidence. Declining is the
    common case early on and is not a failure: a fit produced from too little data
    would be applied to every estimate the engine makes, so the cost of fitting too
    soon is paid on every trade, while the cost of waiting is paid on none.
    """
    if len(samples) < min_samples:
        log.info(
            "calibration.insufficient_samples",
            engine=engine,
            samples=len(samples),
            required=min_samples,
        )
        return None, None

    markets = len({sample.condition_id for sample in samples})
    if markets < min_markets:
        # Counted separately from rows on purpose: the rows are autocorrelated, and
        # this is the check that notices.
        log.info(
            "calibration.insufficient_markets",
            engine=engine,
            samples=len(samples),
            markets=markets,
            required=min_markets,
        )
        return None, None

    calibrator = IsotonicCalibrator(engine, _pool_adjacent_violators(samples))
    before = reliability_bins(samples)
    after = reliability_bins(samples, calibrator=calibrator)
    report = CalibrationReport(
        engine=engine,
        samples=len(samples),
        markets=markets,
        brier_before=brier_score((s.predicted, s.realized) for s in samples),
        brier_after=brier_score((calibrator.apply(s.predicted), s.realized) for s in samples),
        ece_before=expected_calibration_error(before),
        ece_after=expected_calibration_error(after),
        bins=before,
    )
    return calibrator, report
