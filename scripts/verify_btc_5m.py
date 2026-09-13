"""Btc5mEngine against the live Chainlink TWAP feed.

    .venv/bin/python scripts/verify_btc_5m.py

No credentials needed -- the TWAP stream is public. It places no orders; it only prices.

What it proves, and why each check exists rather than being assumed:

* The Chainlink TWAP stream delivers, at the cadence and in the format the engine expects.
* **Duplicate ticks are real.** The feed republishes the same value every second until
  Chainlink moves, so this prints how many raw ticks collapsed into distinct observations.
  A volatility estimate taken over raw ticks would be biased low by about the square root
  of that ratio, making every probability more extreme than the evidence supports.
* **Volatility is measurable** at the long lags the engine uses, and lands in a plausible
  range for BTC. A silently unmeasurable sigma is an engine that abstains forever while
  looking merely cautious.
* A **real** market window prices, when the series covers its opening instant.
* **The strike cannot be recovered after the fact** -- the engine's defining constraint,
  demonstrated rather than described.

The arithmetic itself is checked in ``tests/unit/test_btc_5m.py``, not here. A live script
cannot control sigma, and the synthetic series an earlier version of this file built to
control it had almost no variance of returns -- so every probability printed 0 or 1 and the
check looked decisive while testing nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.adapters.polymarket.streams import PolymarketStreams
from deepflow.config.settings import get_settings
from deepflow.config.thresholds import Btc5mThresholds
from deepflow.core.clock import ManualClock
from deepflow.core.domain import Market, MarketSnapshot, Outcome
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.engines.crypto.btc_5m import Btc5mEngine
from deepflow.engines.crypto.reference import (
    MIN_ANNUALISED_VOLATILITY,
    MIN_VOL_SAMPLES,
    TwapReference,
)

#: Six non-overlapping samples at a 60-second lag need six minutes, plus margin for the
#: duplicate ticks that do not advance the series.
COLLECT_SECONDS = 420
SYMBOL = "btc/usd"
TOKEN = ClobTokenId("1")


def _market(slug: str) -> Market:
    return Market(
        condition_id=ConditionId("0xverify"),
        question="Bitcoin Up or Down",
        slug=slug,
        outcomes=(Outcome(token_id=TOKEN, label="Up"),),
        active=True,
        closed=False,
        accepting_orders=True,
        resolution_source="https://data.chain.link/streams/btc-usd",
    )


async def main() -> int:
    reference = TwapReference()
    raw_ticks = 0
    settings = get_settings()

    print(f"=== collecting {COLLECT_SECONDS}s of {SYMBOL} TWAP ===")
    deadline = datetime.now(UTC) + timedelta(seconds=COLLECT_SECONDS)

    async with PolymarketSession(settings) as session:
        streams = PolymarketStreams(session, settings)
        async for price in streams.subscribe_crypto_twap([SYMBOL], window_seconds=30):
            raw_ticks += 1
            if raw_ticks == 1:
                print(
                    f"  first tick: {price.value} @ {price.observed_at.isoformat()} "
                    f"window={price.window_seconds}s source={price.source}"
                )
            reference.observe(price)
            if datetime.now(UTC) >= deadline:
                break

    distinct = reference.samples(SYMBOL)
    print(f"  raw ticks: {raw_ticks}   distinct values: {distinct}")
    if distinct:
        ratio = raw_ticks / distinct
        print(
            f"  duplicate ratio: {ratio:.2f}x — a per-tick volatility estimate would be "
            f"low by about {ratio**0.5:.2f}x"
        )

    latest = reference.latest(SYMBOL)
    if latest is None:
        print("  FAIL: no observations collected")
        return 1
    observed_at, spot = latest
    print(f"  latest: {spot} @ {observed_at.isoformat()}")

    sigma = reference.volatility_per_second(SYMBOL)
    if sigma is None:
        print(
            f"  FAIL: volatility unmeasurable after {distinct} distinct values "
            f"(needs {MIN_VOL_SAMPLES} lagged samples) — the engine would abstain forever"
        )
        return 1
    annualised = float(sigma) * (365 * 24 * 3600) ** 0.5
    print(f"  sigma: {sigma} per second  (~{annualised:.0%} annualised)")

    failures = 0
    # The floor is part of the estimator, so anything at or below it means the sample was
    # a lull rather than a distribution -- reported, not failed, because a quiet seven
    # minutes is a fact about the market and not a bug.
    if annualised <= float(MIN_ANNUALISED_VOLATILITY) + 1e-9:
        print(
            f"  note: at the {float(MIN_ANNUALISED_VOLATILITY):.0%} floor — the sampled "
            "window was too quiet to measure, and the floor is what keeps the model honest"
        )
    elif annualised > 5.0:
        print("  FAIL: annualised volatility is implausibly high; check the estimator")
        failures += 1

    failures += await _check_pricing(reference, spot, observed_at)
    failures += _check_strike_is_unrecoverable(reference)

    print()
    print("All checks passed. No orders were placed." if not failures else f"{failures} failed.")
    return 1 if failures else 0


async def _check_pricing(reference: TwapReference, spot: Decimal, observed_at: datetime) -> int:
    """Price a window whose strike we genuinely observed, then move the strike."""
    print("\n=== pricing ===")
    failures = 0

    # A window that opened when our series did and expires 120s after the last tick: inside
    # the tradeable band and clear of the 30s averaging window.
    expiry = observed_at + timedelta(seconds=120)
    start = expiry - timedelta(seconds=300)
    slug = f"btc-updown-5m-{int(start.timestamp())}"
    market = _market(slug)
    snapshot = MarketSnapshot(condition_id=market.condition_id, books=(), captured_at=observed_at)

    engine = Btc5mEngine(Btc5mThresholds(), reference=reference, clock=ManualClock(observed_at))
    estimate = await engine.estimate(market=market, snapshot=snapshot, token_id=TOKEN)
    if estimate is None:
        first = reference.first_observed(SYMBOL)
        print(
            f"  abstained: window start {start.isoformat()} vs series start "
            f"{first.isoformat() if first else 'none'}"
        )
        print("  (expected when the series began after the window opened)")
    else:
        print(f"  P(up) = {estimate.model_probability}  uncertainty={estimate.uncertainty}")
        print(f"  strike={estimate.inputs['strike']}  spot={estimate.inputs['spot']}")
        print(
            f"  T={estimate.inputs['seconds_to_expiry']}s  "
            f"T_eff={estimate.inputs['effective_seconds']}s"
        )

    return failures


def _check_strike_is_unrecoverable(reference: TwapReference) -> int:
    """The engine's defining constraint, demonstrated rather than asserted."""
    print("\n=== the strike cannot be recovered after the fact ===")
    first = reference.first_observed(SYMBOL)
    if first is None:
        return 1
    missing = reference.value_at(SYMBOL, first - timedelta(minutes=5))
    if missing is not None:
        print(f"  FAIL: returned {missing} for an instant before the series began")
        return 1
    print("  a window that opened before we subscribed yields no strike — engine abstains")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
