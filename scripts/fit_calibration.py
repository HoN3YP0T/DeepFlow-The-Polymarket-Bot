"""Fit each engine's calibration curve from recorded predictions and outcomes.

    .venv/bin/python scripts/fit_calibration.py                 # report only
    .venv/bin/python scripts/fit_calibration.py --engine btc_5m
    .venv/bin/python scripts/fit_calibration.py --activate      # make the fits live

Offline and read-only by default. Fitting is a measurement; **activating** a fit
changes every probability an engine produces from then on, so it takes an explicit
flag and never happens as a side effect of looking.

The headline output is the reliability table, not the Brier score. A fit can improve
Brier while leaving a six-point gap at 0.95, and 0.95 is where this system trades --
so read the bins, and read the ``markets`` column hardest of all: 400 samples drawn
from 4 markets is four observations wearing a large number.
"""

from __future__ import annotations

import argparse
import asyncio
from decimal import Decimal

from deepflow.adapters.persistence.engine import build_engine, build_session_factory
from deepflow.adapters.persistence.repositories import SqlUnitOfWork
from deepflow.config.settings import Settings
from deepflow.core.domain import CalibrationSample
from deepflow.engines import calibration


def _table(report: calibration.CalibrationReport) -> str:
    lines = [
        f"  {'band':>12}  {'n':>7}  {'mkts':>5}  {'predicted':>9}  {'realized':>9}  {'gap':>7}",
        f"  {'-' * 12}  {'-' * 7}  {'-' * 5}  {'-' * 9}  {'-' * 9}  {'-' * 7}",
    ]
    for row in report.bins:
        lines.append(
            f"  {str(row.lower)[:5]:>5}-{str(row.upper)[:5]:<6}"
            f"  {row.count:>7}  {row.markets:>5}"
            f"  {float(row.mean_predicted):>9.3f}  {float(row.mean_realized):>9.3f}"
            f"  {float(row.gap):>+7.3f}"
        )
    return "\n".join(lines)


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", help="fit only this engine")
    parser.add_argument(
        "--activate",
        action="store_true",
        help="store the fits AND make them live for their engines",
    )
    parser.add_argument(
        "--min-samples", type=int, default=calibration.MIN_SAMPLES, help="override the sample floor"
    )
    parser.add_argument(
        "--min-markets",
        type=int,
        default=calibration.MIN_MARKETS,
        help="override the distinct-market floor -- the one that actually binds",
    )
    args = parser.parse_args()

    settings = Settings()
    engine = build_engine(settings)
    sessions = build_session_factory(engine)

    try:
        async with sessions() as session:
            uow = SqlUnitOfWork(session)
            samples = await uow.predictions.list_samples(engine=args.engine)

        if not samples:
            print("no scored predictions yet.")
            print("  A sample needs a prediction AND a settled market. If the process has")
            print("  been running, check the health line: `predictions` climbing with")
            print("  `settled` stuck at zero means the settlement loop is not recording.")
            return 0

        by_engine: dict[str, list[CalibrationSample]] = {}
        for sample in samples:
            by_engine.setdefault(sample.engine, []).append(sample)

        print(f"{len(samples)} scored predictions across {len(by_engine)} engine(s)\n")
        fitted = 0
        for name in sorted(by_engine):
            group = by_engine[name]
            markets = len({s.condition_id for s in group})
            print(f"=== {name}: {len(group)} samples, {markets} distinct markets")

            calibrator, report = calibration.fit(
                name, group, min_samples=args.min_samples, min_markets=args.min_markets
            )
            if calibrator is None or report is None:
                print(
                    f"  declined: needs {args.min_samples} samples from "
                    f"{args.min_markets} markets. Not a failure -- a fit from too little "
                    "data would be applied to every estimate the engine makes.\n"
                )
                continue

            print(_table(report))
            print(
                f"  brier {float(report.brier_before):.5f} -> {float(report.brier_after):.5f}"
                f"   ece {float(report.ece_before):.5f} -> {float(report.ece_after):.5f}"
            )
            print(f"  curve covers [{calibrator.lower}, {calibrator.upper}]")
            if calibrator.upper < Decimal("0.85"):
                # The band this system targets is 0.85-0.98. A curve that stops short
                # of it is identity exactly where it was needed.
                print("  NOTE: no support in the 0.85-0.98 band -- identity applies there.")
            if not report.improves:
                print("  WARNING: fit is worse than identity on its own training data.")

            if args.activate:
                async with sessions() as session:
                    uow = SqlUnitOfWork(session)
                    await uow.calibration.save(
                        engine=name,
                        knots=calibrator.as_dict(),
                        samples=report.samples,
                        markets=report.markets,
                        brier_before=report.brier_before,
                        brier_after=report.brier_after,
                        ece_before=report.ece_before,
                        ece_after=report.ece_after,
                        activate=True,
                    )
                    await uow.commit()
                print("  ACTIVATED -- this curve now shapes every estimate from this engine.")
            fitted += 1
            print()

        if fitted and not args.activate:
            print("nothing stored. Re-run with --activate to make these curves live.")
        return 0
    finally:
        await engine.dispose()


raise SystemExit(asyncio.run(main()))
