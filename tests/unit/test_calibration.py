"""The calibration fit, and the guards that stop it being fitted on nothing.

The arithmetic is the easy half and is checked here for correctness. The half worth
the tests is the refusals: a curve produced from too little data is applied to every
estimate an engine makes afterwards, so fitting too soon costs something on every
trade while waiting costs nothing on any.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.core.domain import CalibrationSample
from deepflow.engines import calibration
from deepflow.engines.base import BaseProbabilityEngine
from deepflow.engines.calibration import (
    IdentityCalibrator,
    IsotonicCalibrator,
    Knot,
    brier_score,
    fit,
    reliability_bins,
)

NOW = datetime(2026, 9, 14, tzinfo=UTC)


def _sample(predicted: str, realized: int, market: str) -> CalibrationSample:
    return CalibrationSample(
        predicted=Decimal(predicted),
        realized=Decimal(realized),
        engine="test",
        condition_id=market,  # type: ignore[arg-type]
        token_id="tok",  # type: ignore[arg-type]
        predicted_at=NOW,
    )


def _overconfident(band: str, hit_rate: float, count: int, prefix: str) -> list[CalibrationSample]:
    """``count`` predictions at ``band``, right ``hit_rate`` of the time.

    Each from its own market, so the sample is genuinely independent -- which is what
    the fitter demands and what a stream of estimates on one market is not.
    """
    hits = int(count * hit_rate)
    return [_sample(band, 1 if i < hits else 0, f"{prefix}-{i}") for i in range(count)]


def test_identity_adds_no_claim() -> None:
    """The default. An uncalibrated engine must be obvious, not quietly adjusted."""
    identity = IdentityCalibrator()
    assert identity.apply(Decimal("0.97")) == Decimal("0.97")
    # ...and it never claims evidence it does not have.
    assert not identity.supports(Decimal("0.97"))


def test_the_fit_learns_an_overconfident_model() -> None:
    """The failure this whole module exists for: 0.9 that is right 70% of the time.

    A model like this turns a positive edge negative, and a win-rate summary reports
    it as 70 correct out of 100.
    """
    samples = _overconfident("0.6", 0.6, 120, "a") + _overconfident("0.9", 0.7, 120, "b")
    calibrator, report = fit("test", samples, min_samples=10, min_markets=10)
    assert calibrator is not None and report is not None

    assert calibrator.apply(Decimal("0.9")) == pytest.approx(Decimal("0.7"), abs=Decimal("0.01"))
    assert calibrator.apply(Decimal("0.6")) == pytest.approx(Decimal("0.6"), abs=Decimal("0.01"))
    assert report.improves
    assert report.ece_after < report.ece_before


def test_the_curve_cannot_decrease() -> None:
    """Monotonicity is the one assumption isotonic regression makes, and the only
    thing left worth calibrating if it fails: a higher model output must mean a
    higher chance."""
    samples = (
        _overconfident("0.3", 0.8, 60, "a")  # deliberately inverted against...
        + _overconfident("0.7", 0.2, 60, "b")  # ...this band
    )
    calibrator, _ = fit("test", samples, min_samples=10, min_markets=10)
    assert calibrator is not None
    values = [calibrator.apply(Decimal(str(x / 20))) for x in range(21)]
    assert values == sorted(values)


def test_a_fit_never_extrapolates_beyond_what_it_saw() -> None:
    """Outside the observed range the fit returns its input unchanged.

    The alternatives are worse. Extrapolating invents a correction from no evidence,
    and clipping to the nearest fitted value turns missing data into a confident
    claim -- that a 0.97 is 'really' 0.72 because 0.72 is where the samples stopped.
    """
    samples = _overconfident("0.5", 0.5, 60, "a") + _overconfident("0.7", 0.6, 60, "b")
    calibrator, _ = fit("test", samples, min_samples=10, min_markets=10)
    assert calibrator is not None

    assert not calibrator.supports(Decimal("0.97"))
    assert calibrator.apply(Decimal("0.97")) == Decimal("0.97")
    assert not calibrator.supports(Decimal("0.05"))
    assert calibrator.apply(Decimal("0.05")) == Decimal("0.05")


def test_autocorrelated_samples_are_refused_however_many_rows_they_fill() -> None:
    """**The guard that matters.**

    Five hundred predictions drawn from three markets is three coin flips, not five
    hundred observations -- and a curve fitted on them looks beautifully tight around
    a result that had three chances to be wrong. A 5-minute crypto window re-estimated
    on every book update produces exactly this shape.
    """
    samples = [_sample("0.9", i % 2, f"only-{i % 3}") for i in range(500)]
    calibrator, report = fit("test", samples, min_samples=200, min_markets=50)
    assert calibrator is None and report is None


def test_too_few_samples_declines_rather_than_guessing() -> None:
    samples = _overconfident("0.9", 0.7, 20, "a")
    assert fit("test", samples, min_samples=200, min_markets=5) == (None, None)


def test_the_reliability_table_counts_markets_not_only_rows() -> None:
    """A bin holding 400 samples from 4 markets is four observations, and the bins at
    the top of the range are the ones most likely to be one market watched all day."""
    samples = [_sample("0.92", 1, f"m-{i % 4}") for i in range(400)]
    bins = reliability_bins(samples)
    assert len(bins) == 1
    assert bins[0].count == 400
    assert bins[0].markets == 4


def test_the_gap_is_signed_so_overconfidence_is_visible() -> None:
    samples = _overconfident("0.9", 0.7, 100, "a")
    row = reliability_bins(samples)[0]
    # Promised 0.9, delivered 0.7: a +0.2 gap, and the sign is what says which way.
    assert row.gap == pytest.approx(Decimal("0.2"), abs=Decimal("0.01"))


def test_brier_punishes_the_hedge_and_the_shout_alike() -> None:
    """Proper scoring: it is minimised only by reporting the true probability."""
    truth = [(Decimal("0.7"), Decimal(1))] * 70 + [(Decimal("0.7"), Decimal(0))] * 30
    hedge = [(Decimal("0.5"), Decimal(1))] * 70 + [(Decimal("0.5"), Decimal(0))] * 30
    shout = [(Decimal("1"), Decimal(1))] * 70 + [(Decimal("1"), Decimal(0))] * 30
    assert brier_score(truth) < brier_score(hedge)
    assert brier_score(truth) < brier_score(shout)


def test_a_fit_survives_a_round_trip_through_storage() -> None:
    """A stored curve is what the running process applies, so the serialisation is
    load-bearing rather than a convenience."""
    original = IsotonicCalibrator(
        "test",
        [
            Knot(Decimal("0.6"), Decimal("0.55"), 40),
            Knot(Decimal("0.9"), Decimal("0.72"), 60),
        ],
    )
    restored = IsotonicCalibrator.from_dict(original.as_dict())
    for raw in ("0.6", "0.75", "0.9"):
        assert restored.apply(Decimal(raw)) == original.apply(Decimal(raw))


def test_a_malformed_stored_fit_is_rejected_not_silently_emptied() -> None:
    """An empty curve that loaded cleanly would be an identity wearing a fit's name."""
    with pytest.raises(ValueError):
        IsotonicCalibrator.from_dict({"engine": "test", "knots": []})


def test_an_engine_is_identity_until_a_curve_is_installed() -> None:
    """The default has to be the honest one: no fit, no adjustment, and
    ``_calibration_is_supported`` saying so."""

    class _Engine(BaseProbabilityEngine):
        name = "probe"

        async def estimate(self, **_: object) -> None:  # type: ignore[override]
            return None

    engine = _Engine()
    assert engine._calibrate(Decimal("0.97")) == Decimal("0.97")
    assert not engine._calibration_is_supported(Decimal("0.97"))

    engine.use_calibrator(IsotonicCalibrator("probe", [Knot(Decimal("0.9"), Decimal("0.7"), 100)]))
    assert engine._calibrate(Decimal("0.9")) == Decimal("0.7")
    assert engine._calibration_is_supported(Decimal("0.9"))


def test_horizon_is_carried_because_calibration_depends_on_it() -> None:
    """A model five seconds from settlement is a different estimator from the same
    model five minutes out, so a fit that pools every horizon hides the one it is
    worst at."""
    sample = CalibrationSample(
        predicted=Decimal("0.9"),
        realized=Decimal(1),
        engine="test",
        condition_id="c",  # type: ignore[arg-type]
        token_id="t",  # type: ignore[arg-type]
        predicted_at=NOW,
        horizon_seconds=int(timedelta(minutes=4).total_seconds()),
    )
    assert sample.horizon_seconds == 240


def test_module_floors_are_the_defaults() -> None:
    """The overrides exist for experiments; the defaults are what an unattended fit
    uses, and they are the claim this module makes about sufficiency."""
    assert calibration.MIN_SAMPLES == 200
    assert calibration.MIN_MARKETS == 50
