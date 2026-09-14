"""Prove the calibration chain against the live venue.

    .venv/bin/python scripts/verify_calibration.py

Read-only. **Writes nothing to the database**, and that is a deliberate constraint
rather than a convenience: anything written to ``predictions`` becomes evidence a
future fit will train on, and §88 is in this repo because a number somebody typed for
a verification run sailed through every safeguard. A probe must not be able to teach
the system anything.

Three things checked, in order:

1. The resolutions adapter reads settled markets from the live venue at all.
2. **The payout alignment still holds.** ``payouts`` is a positional pair from the
   data API and ``Market.outcomes`` is built from the SDK's named accessors; nothing
   in either says the orders agree, and if they ever stop agreeing every calibration
   sample silently inverts. Checked against what each token last traded at before
   expiry, which is the same measurement that established the alignment (§89).
3. The fitter runs end to end on real outcomes -- using the *market's own* last
   traded price as the prediction. It exercises the whole path on data nobody
   invented, which is the point.

**Read the machinery here, not the curve.** A last-trade price is a biased sample and
the bias was measured rather than assumed: across 88 settled markets the two sides'
last-trade prices summed to a median of **1.030**, with 43% above 1.05 and one at
1.94. They cannot be simultaneous. A losing token stops trading while it is still
plausible, so its "prediction" comes from the middle of the window while its outcome
is final, and every mid-range sample is therefore a loser priced early. That is
exactly what the table shows: the 0.05-0.75 bands realize 0.000 across the board,
which no honest market does.

The one trustworthy row is the top band, where both sides trade to the bell: at
0.95-1.00 the crowd said 0.991 and delivered 0.989 across 91 markets. That is the
baseline any engine here has to beat, and it is a sobering one.

None of this afflicts the real pipeline, which records a prediction with the horizon
it was made at (``Prediction.horizon_seconds``) rather than reconstructing one from
whenever a token last happened to trade.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from deepflow.adapters.polymarket import mapping
from deepflow.adapters.polymarket.resolutions import PolymarketResolutions
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.config.settings import Settings
from deepflow.core.domain import CalibrationSample, Market
from deepflow.engines import calibration

#: How far back to sweep for settled windows. Up/down markets run every five minutes
#: across eight assets, so a few hours is hundreds of genuinely independent outcomes.
LOOKBACK_HOURS = 6

#: Events per page. The venue caps this at 100 whatever the docs say (§65), and the
#: SDK parameter is `page_size`, not `limit`.
PAGE = 100


async def _settled_markets(session: PolymarketSession, now: datetime) -> list[Market]:
    page = await session.public.list_events(
        tag_slug="up-or-down",
        closed=True,
        start_time_min=now - timedelta(hours=LOOKBACK_HOURS),
        start_time_max=now - timedelta(minutes=30),
        page_size=PAGE,
    ).first_page()
    markets: list[Market] = []
    for event in page.items:
        for sdk_market in getattr(event, "markets", None) or ():
            try:
                market = mapping.to_market(sdk_market)
            except Exception as error:
                # A mapping failure here is a venue shape change, which is a finding
                # worth printing rather than a reason to abandon the sweep.
                print(f"  could not map a market: {type(error).__name__}: {error}")
                continue
            if len(market.outcomes) == 2:
                markets.append(market)
    return markets


async def main() -> int:
    failures = 0
    settings = Settings()

    async with PolymarketSession(settings) as session:
        now = datetime.now(UTC)
        markets = await _settled_markets(session, now)
        print(f"settled markets in the last {LOOKBACK_HOURS}h: {len(markets)}")
        if not markets:
            print("FAIL: none found -- the venue runs this cadence continuously")
            return 1

        resolutions = await PolymarketResolutions(session).resolve(markets)
        print(f"resolutions read: {len(resolutions)} of {len(markets)} markets")
        if not resolutions:
            print("FAIL: the adapter read no payouts at all")
            return 1

        by_condition = {str(r.condition_id): r for r in resolutions}

        # --- 2. the alignment ------------------------------------------------
        tokens = [str(o.token_id) for m in markets for o in m.outcomes]
        last = {
            str(p.asset_id): Decimal(str(p.price))
            for p in await session.public.get_last_trade_prices(token_ids=tokens)
        }
        agree = disagree = untraded = 0
        for market in markets:
            resolution = by_condition.get(str(market.condition_id))
            if resolution is None:
                continue
            prices = [last.get(str(o.token_id)) for o in market.outcomes]
            payouts = [resolution.payout_for(o.token_id) for o in market.outcomes]
            if any(p is None for p in prices) or any(p is None for p in payouts):
                untraded += 1
                continue
            won = max(range(len(payouts)), key=lambda i: payouts[i])  # type: ignore[index]
            implied = max(range(len(prices)), key=lambda i: prices[i])  # type: ignore[index]
            agree += won == implied
            disagree += won != implied

        print(f"payout alignment: {agree} agree, {disagree} disagree, {untraded} untraded")
        if disagree:
            # Not a soft warning. A flipped alignment inverts every future sample,
            # and a curve fitted on inverted outcomes teaches the engine to be wrong
            # while looking perfectly well behaved.
            print("FAIL: payout order no longer matches outcome order -- see §89")
            failures += 1

        # --- 3. the fitter, on real outcomes ---------------------------------
        samples: list[CalibrationSample] = []
        for market in markets:
            resolution = by_condition.get(str(market.condition_id))
            if resolution is None:
                continue
            for outcome in market.outcomes:
                price = last.get(str(outcome.token_id))
                payout = resolution.payout_for(outcome.token_id)
                if price is None or payout is None or not (0 <= price <= 1):
                    continue
                samples.append(
                    CalibrationSample(
                        predicted=price,
                        realized=payout,
                        engine="market-last-trade",
                        condition_id=market.condition_id,
                        token_id=outcome.token_id,
                        predicted_at=now,
                    )
                )

        distinct = len({s.condition_id for s in samples})
        print(f"\nsamples built from live outcomes: {len(samples)} across {distinct} markets")
        calibrator, report = calibration.fit(
            "market-last-trade",
            samples,
            # Lowered on purpose: this is a machinery check on one sweep, not a fit
            # anyone should run on. The production floors stay where they are.
            min_samples=20,
            min_markets=10,
        )
        if calibrator is None or report is None:
            print("fitter declined -- too few settled markets in this window; rerun later")
            return failures

        print(f"  brier {float(report.brier_before):.5f} -> {float(report.brier_after):.5f}")
        print(f"  ece   {float(report.ece_before):.5f} -> {float(report.ece_after):.5f}")
        print(f"  curve covers [{calibrator.lower}, {calibrator.upper}]")
        print(f"  {'band':>13}  {'n':>5}  {'mkts':>5}  {'said':>6}  {'was':>6}  {'gap':>7}")
        for row in report.bins:
            print(
                f"  {float(row.lower):>5.2f}-{float(row.upper):<7.2f}"
                f"  {row.count:>5}  {row.markets:>5}"
                f"  {float(row.mean_predicted):>6.3f}  {float(row.mean_realized):>6.3f}"
                f"  {float(row.gap):>+7.3f}"
            )
        if not report.improves:
            print("  NOTE: fit did not beat identity here -- expected when the crowd is")
            print("        already well calibrated, which is itself worth knowing.")

    print("\nOK" if not failures else f"\n{failures} failure(s)")
    return failures


raise SystemExit(asyncio.run(main()))
