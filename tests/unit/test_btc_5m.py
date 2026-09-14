"""BTC 5-minute engine: the first model in this system that produces a number.

Everything here defends one property — that the number is either right or absent. As T
approaches zero the model becomes a step function, so a small error in the reference price
flips the answer from 0.02 to 0.98; that is why the strike, the source and the settlement
convention are read from the venue rather than assumed, and why every mismatch abstains.

The fixtures build reference series with **genuine variance** rather than a linear ramp. A
ramp has almost no variance of returns, so sigma comes out near zero and every probability
prints 0 or 1 — a fixture that would make the engine look decisive while testing nothing.
That is the volatility-domain version of "never build a payload the venue does not send".
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from deepflow.config.thresholds import Btc5mThresholds
from deepflow.core.clock import ManualClock
from deepflow.core.domain import Market, MarketSnapshot, Outcome, ReferencePrice
from deepflow.core.types import ClobTokenId, ConditionId
from deepflow.engines.crypto.btc_5m import (
    MAX_REFERENCE_AGE,
    TWAP_WINDOW_SECONDS,
    Btc5mEngine,
    _parse_window,
    settlement_window_seconds,
)
from deepflow.engines.crypto.reference import (
    MIN_ANNUALISED_VOLATILITY,
    MIN_VOL_SAMPLES,
    SECONDS_PER_YEAR,
    TwapReference,
)

TOKEN = ClobTokenId("1")
SYMBOL = "btc/usd"
WINDOW_START = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
EXPIRY = WINDOW_START + timedelta(seconds=300)
SLUG = f"btc-updown-5m-{int(WINDOW_START.timestamp())}"


#: Live markets name the averaging window in the stream URL. Every one sampled says 60s,
#: including the 5-minute markets — contradicting the changelog's 30s (§85).
LIVE_SOURCE = "https://data.chain.link/streams/btc-usd-twap-60s-streams"


def _market(
    *,
    slug: str = SLUG,
    source: str | None = LIVE_SOURCE,
    text: str | None = None,
) -> Market:
    return Market(
        condition_id=ConditionId("0xcond"),
        question="Bitcoin Up or Down",
        slug=slug,
        outcomes=(Outcome(token_id=TOKEN, label="Up"),),
        active=True,
        closed=False,
        accepting_orders=True,
        resolution_source=source,
        resolution_text=text,
    )


def _snapshot() -> MarketSnapshot:
    return MarketSnapshot(condition_id=ConditionId("0xcond"), books=(), captured_at=WINDOW_START)


def _series(
    *,
    strike: Decimal = Decimal(77000),
    spot: Decimal | None = None,
    wiggle: Decimal = Decimal(40),
    source: str = "chainlink_twap",
    window: int | None = 60,
    start: datetime = WINDOW_START,
    step_seconds: int = 121,
    steps: int = MIN_VOL_SAMPLES + 3,
) -> TwapReference:
    """A reference series that opens exactly at ``strike`` and ends at ``spot``.

    Values alternate around a level by ``wiggle`` so the return series has real variance;
    the final observation is forced to ``spot`` so the engine prices the intended distance.

    ``step_seconds`` is 121 because the volatility estimator samples at twice the averaging
    window and the window is 60 seconds (§85). Six samples therefore need ~12 minutes of
    series — which is also the engine's real warm-up time, doubled from the 6 minutes a
    30-second window implied.
    """
    reference = TwapReference()
    reference.observe(
        ReferencePrice(
            symbol=SYMBOL, value=strike, source=source, window_seconds=window, observed_at=start
        )
    )
    for index in range(1, steps + 1):
        value = strike + (wiggle if index % 2 else -wiggle)
        reference.observe(
            ReferencePrice(
                symbol=SYMBOL,
                value=value,
                source=source,
                window_seconds=window,
                observed_at=start + timedelta(seconds=step_seconds * index),
            )
        )
    if spot is not None:
        reference.observe(
            ReferencePrice(
                symbol=SYMBOL,
                value=spot,
                source=source,
                window_seconds=window,
                observed_at=start + timedelta(seconds=step_seconds * (steps + 1)),
            )
        )
    return reference


def _engine(
    reference: TwapReference, *, now: datetime | None = None, **overrides: object
) -> Btc5mEngine:
    latest = reference.latest(SYMBOL)
    clock_at = now or (latest[0] if latest else WINDOW_START)
    return Btc5mEngine(
        Btc5mThresholds(**overrides),  # type: ignore[arg-type]
        reference=reference,
        clock=ManualClock(clock_at),
    )


async def _price(engine: Btc5mEngine, market: Market | None = None) -> object:
    return await engine.estimate(
        market=market or _market(), snapshot=_snapshot(), token_id=TOKEN
    )


def _at(seconds_before_expiry: int) -> datetime:
    return EXPIRY - timedelta(seconds=seconds_before_expiry)


# --- The window, from the slug --------------------------------------------
def test_the_window_comes_from_the_slug() -> None:
    """`btc-updown-5m-1766162100` is 2025-12-19 16:35Z, matching its own title of
    "11:35AM-11:40AM ET". Neither `start_date` (the listing time, ~24h early) nor the
    localised title is usable."""
    parsed = _parse_window("btc-updown-5m-1766162100")
    assert parsed is not None
    start, length = parsed
    assert start == datetime(2025, 12, 19, 16, 35, tzinfo=UTC)
    assert length == 300


def test_a_fifteen_minute_slug_parses_its_own_length() -> None:
    parsed = _parse_window("eth-updown-15m-1766162100")
    assert parsed is not None
    assert parsed[1] == 900


def test_a_slug_without_a_window_is_not_priced() -> None:
    assert _parse_window("some-other-market") is None


# --- Arithmetic -----------------------------------------------------------
@pytest.mark.asyncio
async def test_at_the_money_prices_near_a_half() -> None:
    reference = _series(strike=Decimal(77000), spot=Decimal(77000))
    estimate = await _price(_engine(reference, now=_at(120)))
    assert estimate is not None
    assert Decimal("0.45") < estimate.model_probability < Decimal("0.55")


@pytest.mark.asyncio
async def test_the_probability_falls_as_the_spot_falls_below_the_strike() -> None:
    above = await _price(_engine(_series(strike=Decimal(77000), spot=Decimal(77100)), now=_at(120)))
    level = await _price(_engine(_series(strike=Decimal(77000), spot=Decimal(77000)), now=_at(120)))
    below = await _price(_engine(_series(strike=Decimal(77000), spot=Decimal(76900)), now=_at(120)))
    assert above is not None and level is not None and below is not None
    assert below.model_probability < level.model_probability < above.model_probability


@pytest.mark.asyncio
async def test_the_same_distance_is_more_decisive_closer_to_expiry() -> None:
    """The whole reason this engine exists: with less time left, the same gap to the strike
    is harder to close."""
    early = await _price(_engine(_series(strike=Decimal(77000), spot=Decimal(77100)), now=_at(280)))
    # 70s, not 60: with a 60-second averaging window the engine refuses at or inside it.
    late = await _price(_engine(_series(strike=Decimal(77000), spot=Decimal(77100)), now=_at(70)))
    assert early is not None and late is not None
    assert late.model_probability > early.model_probability


@pytest.mark.asyncio
async def test_the_effective_horizon_is_shorter_than_the_calendar_one() -> None:
    """Settlement is the *average* over the final 30 seconds, not the endpoint, and the
    variance of an average over [T-w, T] is sigma^2 (T - 2w/3). A spot-priced model uses T
    and overstates uncertainty by about 20% at a two-minute horizon."""
    estimate = await _price(_engine(_series(spot=Decimal(77000)), now=_at(120)))
    assert estimate is not None
    assert Decimal(estimate.inputs["seconds_to_expiry"]) == Decimal(120)
    # 2w/3 with w=60, so 40 seconds of the horizon are consumed by the averaging.
    assert Decimal(estimate.inputs["effective_seconds"]) == Decimal(120) - Decimal(40)


@pytest.mark.asyncio
async def test_uncertainty_widens_as_expiry_approaches() -> None:
    """Opposite to the model's own confidence: as T shrinks the probability approaches a
    step function, so the same four-second-old price moves the answer further."""
    early = await _price(_engine(_series(spot=Decimal(77000)), now=_at(280)))
    late = await _price(_engine(_series(spot=Decimal(77000)), now=_at(70)))
    assert early is not None and late is not None
    assert late.uncertainty > early.uncertainty


# --- Abstentions ----------------------------------------------------------
@pytest.mark.asyncio
async def test_it_abstains_inside_the_averaging_window() -> None:
    """Part of the settlement average is already realized and the published rolling average
    does not say which part, so the remaining uncertainty is not recoverable."""
    reference = _series(spot=Decimal(77000))
    engine = _engine(reference, now=_at(TWAP_WINDOW_SECONDS - 1), min_seconds_to_expiry=5)
    assert await _price(engine) is None


@pytest.mark.asyncio
async def test_it_abstains_when_the_strike_was_not_observed() -> None:
    """The engine's defining constraint: no field carries the opening price, so it must
    have been watching. A later price is not a substitute — the strike is one instant's
    value, and swapping it turns a comparison into a guess."""
    late_series = _series(start=WINDOW_START + timedelta(seconds=30), spot=Decimal(77000))
    assert await _price(_engine(late_series, now=_at(120))) is None


@pytest.mark.asyncio
async def test_it_abstains_on_a_stale_reference_however_fresh_the_book_is() -> None:
    """The book can be perfectly live while the price this market settles on has stopped
    arriving, and that is exactly when a confident probability is worthless."""
    reference = _series(spot=Decimal(77000))
    latest = reference.latest(SYMBOL)
    assert latest is not None
    stale_now = latest[0] + MAX_REFERENCE_AGE + timedelta(seconds=1)
    assert await _price(_engine(reference, now=stale_now)) is None


@pytest.mark.asyncio
async def test_it_abstains_on_a_spot_series() -> None:
    """A market settling on a Chainlink TWAP priced off Binance spot is wrong by the basis
    between them, and at five minutes that basis is the whole edge."""
    spot_series = _series(spot=Decimal(77000), source="binance", window=None)
    assert await _price(_engine(spot_series, now=_at(120))) is None


@pytest.mark.asyncio
async def test_it_abstains_when_the_market_does_not_name_the_chainlink_stream() -> None:
    reference = _series(spot=Decimal(77000))
    other = _market(source="https://example.com/prices", text="resolves on some other feed")
    assert await _price(_engine(reference, now=_at(120)), other) is None


@pytest.mark.asyncio
async def test_the_resolution_text_alone_is_enough_to_identify_the_source() -> None:
    """Live markets state it in both fields; either naming the stream is enough."""
    reference = _series(spot=Decimal(77000))
    described = _market(
        source=None,
        text=(
            "The resolution source is the Chainlink BTC/USD TWAP stream at "
            "data.chain.link/streams/btc-usd-twap-60s-streams"
        ),
    )
    assert await _price(_engine(reference, now=_at(120)), described) is not None


@pytest.mark.asyncio
async def test_it_abstains_on_an_unknown_asset() -> None:
    reference = _series(spot=Decimal(77000))
    unknown = _market(slug=f"pepe-updown-5m-{int(WINDOW_START.timestamp())}")
    assert await _price(_engine(reference, now=_at(120)), unknown) is None


@pytest.mark.asyncio
async def test_it_abstains_outside_the_configured_expiry_band() -> None:
    reference = _series(spot=Decimal(77000))
    assert await _price(_engine(reference, now=_at(299), max_seconds_to_expiry=200)) is None


@pytest.mark.asyncio
async def test_it_abstains_when_volatility_is_unmeasurable() -> None:
    """A zero sigma is the most dangerous output available here — it makes every
    probability exactly 0 or 1 — so it abstains rather than flooring to something tiny."""
    thin = TwapReference()
    thin.observe(
        ReferencePrice(
            symbol=SYMBOL,
            value=Decimal(77000),
            source="chainlink_twap",
            window_seconds=60,
            observed_at=WINDOW_START,
        )
    )
    assert await _price(_engine(thin, now=_at(120))) is None


# --- The reference series -------------------------------------------------
def test_duplicate_ticks_are_not_new_measurements() -> None:
    """The feed republishes the same value every second until Chainlink moves. Measured
    live at a 1.19x duplicate ratio; keeping them would make the series look longer and
    the market calmer than it is."""
    reference = TwapReference()
    for second in range(6):
        reference.observe(
            ReferencePrice(
                symbol=SYMBOL,
                value=Decimal(77000),
                source="chainlink_twap",
                window_seconds=60,
                observed_at=WINDOW_START + timedelta(seconds=second),
            )
        )
    assert reference.samples(SYMBOL) == 1


def test_volatility_is_floored_rather_than_trusted_when_implausibly_low() -> None:
    """Measured live: seven minutes of BTC produced 4% annualised — true of those minutes
    and badly wrong about the next two. Sigma is the denominator, so an underestimate
    pushes 0.8 to 0.99. A floor is conservative: a larger sigma pulls probabilities toward
    0.5."""
    calm = _series(strike=Decimal(77000), wiggle=Decimal("0.01"), steps=MIN_VOL_SAMPLES + 3)
    sigma = calm.volatility_per_second(SYMBOL)
    assert sigma is not None
    floor = MIN_ANNUALISED_VOLATILITY / Decimal(str(float(SECONDS_PER_YEAR) ** 0.5))
    assert sigma == floor


def test_a_real_move_is_reported_above_the_floor() -> None:
    """The floor must not swallow a genuinely volatile series."""
    wild = _series(strike=Decimal(77000), wiggle=Decimal(2000), steps=MIN_VOL_SAMPLES + 3)
    sigma = wild.volatility_per_second(SYMBOL)
    floor = MIN_ANNUALISED_VOLATILITY / Decimal(str(float(SECONDS_PER_YEAR) ** 0.5))
    assert sigma is not None
    assert sigma > floor


def test_a_strike_before_the_series_began_is_unrecoverable() -> None:
    reference = _series(spot=Decimal(77000))
    assert reference.value_at(SYMBOL, WINDOW_START - timedelta(minutes=5)) is None


def test_a_carried_forward_value_answers_for_a_later_instant() -> None:
    """Chainlink holds a value until it changes, so the last observation at or before an
    instant is the value in force there."""
    reference = _series(strike=Decimal(77000), spot=Decimal(77100))
    assert reference.value_at(SYMBOL, WINDOW_START + timedelta(seconds=30)) == Decimal(77000)


def test_out_of_order_ticks_are_dropped() -> None:
    """The series is read by index for the strike lookup, and an unsorted deque returns the
    wrong instant's price."""
    reference = TwapReference()
    for value, offset in ((Decimal(77000), 0), (Decimal(77100), 60), (Decimal(76900), 30)):
        reference.observe(
            ReferencePrice(
                symbol=SYMBOL,
                value=value,
                source="chainlink_twap",
                window_seconds=60,
                observed_at=WINDOW_START + timedelta(seconds=offset),
            )
        )
    assert reference.samples(SYMBOL) == 2
    assert reference.latest(SYMBOL) == (WINDOW_START + timedelta(seconds=60), Decimal(77100))


def test_the_volatility_memo_is_invalidated_by_a_new_observation() -> None:
    """Exact, not time-based: a cached sigma must always be the sigma the current series
    implies.

    The memo exists because the estimate is O(series) — up to ~1,800 observations — and was
    being recomputed for every market on every book update, starving the feed (§81). Dozens
    of snapshots arrive between two Chainlink publications, so most of those recomputations
    returned an identical number.
    """
    reference = _series(strike=Decimal(77000), wiggle=Decimal(40))
    first = reference.volatility_per_second(SYMBOL)
    assert first is not None
    # Same series, so the same answer, and it must come from the memo rather than a rerun.
    assert reference.volatility_per_second(SYMBOL) == first

    latest = reference.latest(SYMBOL)
    assert latest is not None
    reference.observe(
        ReferencePrice(
            symbol=SYMBOL,
            value=Decimal(90000),
            source="chainlink_twap",
            window_seconds=60,
            observed_at=latest[0] + timedelta(seconds=121),
        )
    )
    # A 17% jump cannot leave the estimate unchanged; a stale memo would say it did.
    assert reference.volatility_per_second(SYMBOL) != first


def test_an_unmeasurable_volatility_is_cached_too() -> None:
    """"Not enough history yet" is as expensive to recompute and as stable as a number."""
    thin = TwapReference()
    thin.observe(
        ReferencePrice(
            symbol=SYMBOL,
            value=Decimal(77000),
            source="chainlink_twap",
            window_seconds=60,
            observed_at=WINDOW_START,
        )
    )
    thin.observe(
        ReferencePrice(
            symbol=SYMBOL,
            value=Decimal(77010),
            source="chainlink_twap",
            window_seconds=60,
            observed_at=WINDOW_START + timedelta(seconds=121),
        )
    )
    assert thin.volatility_per_second(SYMBOL) is None
    assert thin.volatility_per_second(SYMBOL) is None


def test_the_settlement_window_is_read_from_the_market_not_assumed() -> None:
    """Every live market names it in the stream URL, and the changelog's figure is wrong.

    §63 (the venue's own changelog) says 5-minute markets use a 30-second lookback. All 56
    live markets sampled — 32 of them 5-minute — cite `…-twap-60s-streams` and say the market
    "is about the price according to the TWAP Chainlink data stream". The resolution text is
    what the market pays on, so it wins (§85).
    """
    assert settlement_window_seconds(_market(source=LIVE_SOURCE)) == 60
    assert settlement_window_seconds(_market(source=None, text="… twap-30s-streams …")) == 30


def test_a_market_that_names_no_window_is_not_priced() -> None:
    """Assuming one is the §63 mistake: pricing a different variable with no symptom."""
    assert settlement_window_seconds(_market(source="https://example.com/feed", text="")) is None


@pytest.mark.asyncio
async def test_it_abstains_when_the_window_it_holds_is_not_the_one_that_settles() -> None:
    """A 30-second average is not a 60-second one. At these horizons the difference between
    them is a meaningful share of the whole edge."""
    thirty_second_series = _series(spot=Decimal(77000), window=30)
    assert await _price(_engine(thirty_second_series, now=_at(120))) is None
