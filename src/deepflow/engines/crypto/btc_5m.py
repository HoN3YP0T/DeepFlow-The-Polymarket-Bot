"""BTC 5-minute market model. Section 10.

A separate strategy, not a fast version of the crypto model. The horizon is so
short that drift is negligible and the entire probability is a barrier problem:
given spot S, strike K, time-to-expiry T and short-horizon volatility sigma,
what is P(S_T > K)?

The failure mode this engine exists to avoid: as T approaches zero the model
probability approaches a step function, so a small error in the *reference
price* -- the wrong feed, a stale tick, spot instead of the TWAP the market
settles against -- flips the answer from 0.02 to 0.98. That is why the strike,
the reference source and the settlement convention are read from the
resolution criteria rather than assumed, and why a mismatch is an abstention.

These markets are never described as sure-shot. A 97% probability held 30
times is an expected loss, and the late-expiry entries this engine looks for
are exactly where that arithmetic bites.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from math import erf, sqrt
from math import log as ln
from typing import Final

from deepflow.config.thresholds import Btc5mThresholds
from deepflow.core.clock import Clock, SystemClock
from deepflow.core.domain import (
    Market,
    MarketSnapshot,
    ProbabilityEstimate,
    ReferencePrice,
)
from deepflow.core.enums import MarketCategory
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId
from deepflow.engines.base import BaseProbabilityEngine
from deepflow.engines.crypto.reference import TwapReference

log = get_logger(__name__)

#: The averaging window every live up/down market names in its own resolution text.
#:
#: **60 seconds, including the 5-minute ones.** The venue's changelog says 5-minute markets
#: use a 30-second lookback (§63) and the markets themselves contradict it: all 56 live
#: markets sampled -- 32 of them 5-minute -- cite
#: ``data.chain.link/streams/<pair>-twap-60s-streams`` and state that the market "is about
#: the price according to the TWAP Chainlink data stream". The resolution text is what the
#: market pays on, so it wins over the changelog (§85).
#:
#: Read per market by :func:`settlement_window_seconds` rather than assumed from this
#: constant; it exists to say what the feed should subscribe to.
TWAP_WINDOW_SECONDS: Final = 60

#: Matches the averaging window in a Chainlink stream URL, e.g. ``btc-usd-twap-60s-streams``.
TWAP_WINDOW_IN_SOURCE = re.compile(r"twap-(\d{1,3})s", re.I)


#: The venue's slug for these markets ends in the window's **start** epoch.
#:
#: ``btc-updown-5m-1766162100`` -> 2025-12-19 16:35:00Z, matching its own title of
#: "11:35AM-11:40AM ET". The machine-readable route to the window, and the only reliable
#: one: the title is a localised human string, and ``start_date`` is when the market
#: *listed*, roughly 24 hours earlier (§54).
SLUG_WINDOW = re.compile(r"-(\d+)m-(\d{9,11})$")

#: Chainlink's quote currency for the up/down cadence. Every published pair is against USD.
#:
#: The symbol is **derived** from the market's slug rather than looked up in a table, and
#: that is a correction rather than a shortcut: a hardcoded list of eight assets had three
#: wrong (ada, link, avax are not published) and was missing three the venue actually runs
#: (bnb, hype, zec) — §79. Subscribing without a symbol filter gives exactly what the venue
#: publishes, and an asset it adds tomorrow then works with no release.
#:
#: Slash-delimited and lowercase, which is Chainlink's format and **not** Binance's
#: (``btcusdt``). The two are not interchangeable and the wrong one silently prices a
#: different instrument (§63).
CHAINLINK_QUOTE: Final = "usd"

#: The source this engine is willing to price against.
#:
#: Checked against the market's own stated resolution source before estimating. A market
#: settling on Chainlink priced off anything else is mispriced by the basis between them,
#: and at a 5-minute horizon that basis is the entire edge.
REQUIRED_SOURCE: Final = "chainlink_twap"

#: Substring identifying the Chainlink data stream in a market's resolution source URL.
CHAINLINK_SOURCE_HINT: Final = "chain.link"

#: How stale a reference tick may be before the engine abstains.
#:
#: The feed's effective cadence is about two seconds, so four allows one missed update.
#: Freshness of the *order book* is irrelevant here: the book can be perfectly live while
#: the price this market settles on has stopped arriving, and that is precisely the
#: situation in which a confident probability is worthless.
MAX_REFERENCE_AGE = timedelta(seconds=4)


class Btc5mEngine(BaseProbabilityEngine):
    """Barrier-crossing probability for short-dated crypto up/down markets.

    What the venue actually settles on, established live rather than assumed:

    * The market resolves **Up** if the price at the end of the window is **greater than
      or equal to** the price at its beginning. Equality favours Up -- a perfectly flat
      window is not a coin flip.
    * Both prices come from the Chainlink data stream, which per the venue's changelog
      publishes a **30-second TWAP** for 5-minute markets (§63). So the settlement value
      is the average of the final 30 seconds, not the last tick.
    * **No field carries the strike.** Dumping every attribute of a live market shows no
      opening price anywhere: it is implicit in "the price at the beginning of that
      range" (§77). The engine therefore has to have *watched* the window open, and
      abstains when it did not.

    That last point is the engine's defining constraint and the reason it abstains so
    often. It cannot be worked around by reading a later price -- the strike is a specific
    instant's value, and substituting any other number turns a comparison into a guess.
    """

    name = "btc_5m"
    categories = frozenset({MarketCategory.BTC_5M})

    def __init__(
        self,
        thresholds: Btc5mThresholds,
        *,
        reference: TwapReference | None = None,
        clock: Clock | None = None,
    ) -> None:
        self._thresholds = thresholds
        self._reference = reference or TwapReference()
        self._clock = clock or SystemClock()

    def observe(self, price: ReferencePrice) -> None:
        """Feed one reference observation into the history this engine prices from."""
        self._reference.observe(price)

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """Estimate P(settles at or above the opening price).

        The whole calculation, and why each piece is what it is:

        ``P = Phi( ln(S / K) / (sigma * sqrt(T_eff)) )``

        * **K**, the strike, is the TWAP observed **at the window's start**. Unpublished,
          so it must have been watched.
        * **S** is the latest TWAP. Under a driftless random walk the expected future
          average equals the current value, so the distribution is centred here. Drift is
          genuinely negligible over 300 seconds -- at any plausible annual rate it is
          orders of magnitude below one standard deviation of five-minute noise -- and
          including a guess at it would add error, not accuracy.
        * **sigma** is realized volatility from the TWAP series, estimated at long lags
          for the reasons in :mod:`deepflow.engines.crypto.reference`.
        * **T_eff = T - 2w/3**, and this is the part a spot-priced model gets wrong.
          Settlement is the *average* over the final ``w`` seconds, not the endpoint, and
          the variance of the average of a random walk over ``[T-w, T]`` is
          ``sigma^2 (T - w + w/3)``. Averaging destroys variance, so the effective horizon
          is shorter than the calendar one: at T=60s with a 30s window, 40s rather than
          60s. Using T would overstate uncertainty by about 20% and systematically
          understate every probability near the money.

        **Abstains** -- each case a way the number would otherwise be confidently wrong:

        * the market's resolution source is not the Chainlink stream this engine tracks;
        * the strike was not observed, because we were not watching when the window opened;
        * the reference tick is stale, however fresh the order book is;
        * ``T <= w``: part of the settlement average is already realized and the feed does
          not say which part, so the remaining uncertainty is not recoverable;
        * time to expiry is outside the configured band;
        * volatility is unmeasurable, or measures zero.

        A zero sigma is the most dangerous output available here -- it makes every
        probability exactly 0 or 1 -- which is why it abstains rather than flooring.
        """
        window = _parse_window(market.slug)
        if window is None:
            return None
        symbol = _chainlink_symbol(market.slug)
        if symbol is None:
            return None
        if not _settles_on_chainlink(market):
            log.info("btc_5m.source_mismatch", slug=market.slug, source=market.resolution_source)
            return None

        start, length = window
        expiry = start + timedelta(seconds=length)
        now = self._clock.now()
        seconds_left = (expiry - now).total_seconds()

        if not (
            self._thresholds.min_seconds_to_expiry
            <= seconds_left
            <= self._thresholds.max_seconds_to_expiry
        ):
            return None

        twap_window = settlement_window_seconds(market)
        if twap_window is None:
            # The market does not name its averaging window, so the settlement variable is
            # unknown. Assuming one is precisely the §63 mistake -- pricing a different
            # instrument -- and the error is largest exactly where this engine trades.
            log.info("btc_5m.settlement_window_unknown", slug=market.slug)
            return None

        held = self._reference.window_of(symbol)
        if held != twap_window:
            # The series we hold averages over a different window than the market settles
            # on. A 30-second average is not a 60-second one, and at these horizons the
            # difference between them is a meaningful share of the edge.
            log.info(
                "btc_5m.window_mismatch", symbol=symbol, market=twap_window, reference=held
            )
            return None

        if seconds_left <= twap_window:
            # The settlement average already includes time we cannot separate from the
            # published rolling average. Abstaining here is why the configured
            # ``min_seconds_to_expiry`` floor must sit above the TWAP window -- see
            # ``Btc5mThresholds``.
            # Debug, not info: this fires on every snapshot of every market in its final
            # 30 seconds, and a measured run produced thousands of identical lines that
            # buried the decisions that did happen. The health line's counters carry the
            # signal instead (§81).
            log.debug("btc_5m.inside_averaging_window", seconds_left=seconds_left)
            return None

        latest = self._reference.latest(symbol)
        if latest is None:
            return None

        tracked_source = self._reference.source_of(symbol)
        if tracked_source != REQUIRED_SOURCE:
            # The market settles on a Chainlink TWAP, so a Binance spot series is a
            # different quantity about the same asset. Held separately and refused here
            # rather than substituted: the basis between them is the entire edge at a
            # five-minute horizon (§63).
            log.info("btc_5m.wrong_reference_source", symbol=symbol, tracked=tracked_source)
            return None

        observed_at, spot = latest
        if now - observed_at > MAX_REFERENCE_AGE:
            log.debug("btc_5m.stale_reference", symbol=symbol, age=str(now - observed_at))
            return None

        strike = self._reference.value_at(symbol, start)
        if strike is None:
            # The common case in practice: this process was not subscribed when the window
            # opened. Unrecoverable rather than approximable.
            log.debug("btc_5m.strike_unobserved", symbol=symbol, window_start=start.isoformat())
            return None

        sigma = self._reference.volatility_per_second(symbol)
        if sigma is None or sigma <= 0:
            log.debug(
                "btc_5m.volatility_unmeasurable",
                symbol=symbol,
                samples=self._reference.samples(symbol),
            )
            return None

        effective = Decimal(str(seconds_left)) - Decimal(twap_window) * 2 / 3
        if effective <= 0:
            return None

        raw = _probability_above(spot=spot, strike=strike, sigma=sigma, seconds=effective)
        uncertainty = self._uncertainty(seconds=effective, raw=raw)

        return ProbabilityEstimate(
            token_id=token_id,
            model_probability=raw,
            calibrated_probability=self._calibrate(raw),
            uncertainty=uncertainty,
            engine=self.name,
            inputs={
                "symbol": symbol,
                "strike": str(strike),
                "spot": str(spot),
                "sigma_per_second": str(sigma),
                "seconds_to_expiry": str(seconds_left),
                "effective_seconds": str(effective),
                "twap_window_seconds": str(twap_window),
                "reference_age_seconds": str((now - observed_at).total_seconds()),
                "reference_samples": str(self._reference.samples(symbol)),
                "reference_source": tracked_source,
            },
        )

    def _uncertainty(self, *, seconds: Decimal, raw: Decimal) -> Decimal:
        """Uncertainty that **widens as expiry approaches**, opposite to the model's own
        confidence.

        As T shrinks the probability approaches a step function, so the same staleness in
        the reference price moves the answer further: at 200 seconds a four-second-old
        price is noise, and at 40 seconds it can be the difference between 0.6 and 0.95.

        The term is ``sqrt(reference age / effective horizon)``, weighted by how steep the
        CDF is at this probability. Note what it is *not*: an earlier version framed this
        as "the price move one staleness interval of drift would produce at the measured
        volatility", and the sigma in that framing **cancels** -- a move of
        ``sigma*sqrt(age)`` divided by a scale of ``sigma*sqrt(T)`` is just
        ``sqrt(age/T)``. The code is unchanged in value and the description now matches
        it, because a comment that overstates what a number knows is worse than none.

        The weight is largest near the money and smallest in the tails, which is where the
        CDF is steep: at 0.5 a small price error is a large probability error, and at 0.99
        it is not.
        """
        staleness = Decimal(str(sqrt(MAX_REFERENCE_AGE.total_seconds() / float(seconds))))
        return min(
            Decimal(1), self._thresholds.uncertainty_buffer + staleness * _price_error_weight(raw)
        )

    def _short_horizon_volatility(self, snapshot: MarketSnapshot) -> Decimal | None:
        """Realized volatility over the relevant horizon.

        Kept for the port's shape and delegating to the reference series rather than the
        snapshot: the order book is not the settlement feed, and measuring volatility from
        market prices would make the model's input a function of its own output.
        """
        symbol = _chainlink_symbol(getattr(snapshot, "slug", None))
        return self._reference.volatility_per_second(symbol) if symbol else None


def settlement_window_seconds(market: Market) -> int | None:
    """The averaging window the market says it settles on, or ``None`` if it does not say.

    Read from the resolution text and source, where the venue names the stream explicitly
    (``…-twap-60s-streams``). ``None`` rather than a default, because the default was wrong:
    the changelog's "30 seconds for 5-minute markets" (§63) is contradicted by every live
    market's own text, and an engine that assumed 30 while the market settled on 60 would be
    pricing the wrong variable with no symptom (§85).
    """
    haystack = f"{market.resolution_text or ''} {market.resolution_source or ''}"
    match = TWAP_WINDOW_IN_SOURCE.search(haystack)
    return int(match.group(1)) if match else None


def _parse_window(slug: str | None) -> tuple[datetime, int] | None:
    """``(window start, length in seconds)`` from the market slug.

    ``btc-updown-5m-1766162100`` -> ``(2025-12-19 16:35:00Z, 300)``. Both halves come from
    the slug because both are wrong elsewhere: ``start_date`` is the listing time, ~24
    hours early (§54), and the title's window is a localised human string.
    """
    if not slug:
        return None
    match = SLUG_WINDOW.search(slug)
    if match is None:
        return None
    minutes, epoch = int(match.group(1)), int(match.group(2))
    return datetime.fromtimestamp(epoch, UTC), minutes * 60


def _chainlink_symbol(slug: str | None) -> str | None:
    """Chainlink's symbol for the asset a slug names, derived rather than looked up.

    ``btc-updown-5m-...`` -> ``btc/usd``. No allowlist, because an allowlist is a second
    place for the truth to live and the first copy was wrong about three of its eight
    entries (§79). An asset the venue has not published simply has no series in the
    reference, so :meth:`TwapReference.latest` returns ``None`` and the engine abstains --
    the same answer the allowlist gave, reached without a list to maintain.
    """
    if not slug:
        return None
    prefix = slug.split("-", 1)[0].strip().lower()
    if not prefix or not prefix.isalnum():
        return None
    return f"{prefix}/{CHAINLINK_QUOTE}"


def _settles_on_chainlink(market: Market) -> bool:
    """Whether the market itself says it settles on the Chainlink stream.

    Read from the market rather than assumed from its category. Live markets state it in
    both ``resolution_source`` and the description; either naming the stream is enough,
    and neither naming it is an abstention.
    """
    haystack = " ".join(
        fragment.lower()
        for fragment in (market.resolution_source or "", market.resolution_text or "")
    )
    return CHAINLINK_SOURCE_HINT in haystack


def _probability_above(
    *, spot: Decimal, strike: Decimal, sigma: Decimal, seconds: Decimal
) -> Decimal:
    """``Phi( ln(spot/strike) / (sigma * sqrt(seconds)) )``, clamped into [0, 1].

    Driftless on purpose: over 300 seconds any plausible drift is orders of magnitude
    below one standard deviation of the noise, so including an estimate of it would add
    error rather than accuracy.

    Equality resolves **Up**, so the boundary belongs to this side. For a continuous
    distribution that adds no mass, and it is recorded because it decides the sign of the
    tie -- a perfectly flat window pays Up, not nothing.
    """
    denominator = float(sigma) * sqrt(float(seconds))
    if denominator <= 0:
        return Decimal("0.5")
    z = ln(float(spot) / float(strike)) / denominator
    return _clamp(Decimal(str(_normal_cdf(z))))


def _normal_cdf(z: float) -> float:
    """Standard normal CDF via ``erf``, which is exact enough and in the stdlib.

    An approximation with a stated error of 1e-7 is far below the error in sigma, so
    precision here is not the binding constraint -- a dependency would be.
    """
    return 0.5 * (1.0 + erf(z / sqrt(2.0)))


def _clamp(value: Decimal) -> Decimal:
    return min(Decimal(1), max(Decimal(0), value))


def _price_error_weight(raw: Decimal) -> Decimal:
    """How much a reference-price error matters at this probability.

    Largest near the money and smallest in the tails, because that is where the CDF is
    steep: at 0.5 a small price move is a large probability move, and at 0.99 it is not.
    Scaled so the uncertainty added is meaningful without swamping the configured buffer.
    """
    return Decimal(4) * raw * (Decimal(1) - raw)
