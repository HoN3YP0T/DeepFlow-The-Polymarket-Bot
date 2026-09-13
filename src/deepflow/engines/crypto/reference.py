"""Reference-price history for short-dated crypto markets.

Holds the Chainlink TWAP series a 5-minute up/down market settles against, and answers
the three questions the model needs: what is it now, what was it at the window's start
(the strike), and how volatile has it been.

Two measured properties of the live feed shape everything here, and both bias a naive
reading toward **overconfidence** -- which at these horizons means a probability closer to
0 or 1 than the evidence supports:

1. **Ticks arrive every second, but the value changes about every two.** Measured live:
   consecutive 1-second ticks repeat the same value verbatim. Computing returns over every
   tick therefore inserts a zero for roughly half of them and halves the variance estimate
   (§76).
2. **A TWAP is a smoothed series.** The change in a 30-second moving average over one
   second is far smaller than the underlying price's one-second move, so short-lag returns
   of a TWAP measure the averaging, not the market. Volatility is therefore estimated at
   lags long relative to the window, where the smoothing bias is second-order.
"""

from __future__ import annotations

from bisect import bisect_left
from collections import deque
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from math import log as ln
from math import sqrt
from typing import Final

from deepflow.core.domain import ReferencePrice
from deepflow.core.logging import get_logger

log = get_logger(__name__)

#: How much history to keep per symbol.
#:
#: Long enough to estimate volatility at lags well above the averaging window and across
#: several market windows; short enough that an hour-old regime does not dominate a
#: 5-minute forecast.
HISTORY = timedelta(minutes=30)

#: Shortest lag used for a volatility estimate, as a multiple of the TWAP window.
#:
#: Two. At a lag of one second the change in a 30-second average is dominated by the
#: averaging itself; at 60 seconds the smoothing contributes a second-order correction
#: (roughly ``1 - lag/(3w)``, which is already small at ``lag = 2w``) and the estimate
#: measures the market instead.
MIN_LAG_WINDOWS: Final = 2

#: Volatility needs at least this many non-overlapping lagged returns to mean anything.
#:
#: Six samples is a weak estimator and is treated as a floor rather than a target: below
#: it the engine abstains, because an underestimated sigma at a 60-second horizon produces
#: a probability of 0.99 where the honest answer is 0.8.
MIN_VOL_SAMPLES: Final = 6

#: Smallest annualised volatility this estimator will report.
#:
#: Measured live: seven minutes of BTC TWAP produced **4% annualised** — the price moved
#: $10 on $77,000. That figure is not wrong about those seven minutes and is badly wrong
#: about the next two, and the error runs in the dangerous direction: sigma is the
#: denominator, so halving it pushes a 0.8 probability to 0.99 (§78).
#:
#: A floor, not an abstention, because it is **conservative in the only direction that
#: matters** — a larger sigma pulls every probability toward 0.5 and widens no position
#: that should not be widened. 20% annualised is roughly the quietest sustained regime BTC
#: has traded in; below that the estimator is describing a lull, not a distribution.
MIN_ANNUALISED_VOLATILITY: Final = Decimal("0.20")

#: Seconds in a year, for annualising a per-second volatility.
SECONDS_PER_YEAR: Final = Decimal(365 * 24 * 3600)

#: How far from the requested instant a stored observation may sit and still answer for it.
#:
#: Two seconds, because the feed's effective cadence is about two seconds. Wider would let
#: a tick from the wrong side of the window boundary stand in as the strike, and the strike
#: is the one number a 5-minute market is entirely a comparison against.
STRIKE_TOLERANCE = timedelta(seconds=2)


class TwapReference:
    """Rolling TWAP history for one or more symbols.

    Deliberately not persisted. A gap across a restart is a gap in the evidence, and
    reloading yesterday's series to fill it would produce a volatility estimate describing
    a session that has ended.
    """

    def __init__(self, *, history: timedelta = HISTORY) -> None:
        self._history = history
        self._series: dict[str, deque[tuple[datetime, Decimal]]] = {}
        self._source: dict[str, str] = {}
        self._windows: dict[str, int | None] = {}

    def observe(self, price: ReferencePrice) -> None:
        """Record an observation, dropping the duplicate ticks the feed carries forward.

        A repeated value is not a new measurement -- the feed emits the same number every
        second until Chainlink publishes a new one -- and keeping it would make the series
        look twice as long and half as volatile as it is. The *timestamps* of real changes
        are what the volatility estimate needs.
        """
        series = self._series.setdefault(price.symbol, deque())
        self._source[price.symbol] = price.source
        self._windows[price.symbol] = price.window_seconds

        if series and series[-1][1] == price.value:
            return
        if series and price.observed_at <= series[-1][0]:
            # Out-of-order or replayed tick. Dropped rather than inserted: the series is
            # read by index for the strike lookup, and an unsorted deque would return the
            # wrong instant's price.
            log.debug("reference.out_of_order", symbol=price.symbol)
            return

        series.append((price.observed_at, price.value))
        cutoff = price.observed_at - self._history
        while series and series[0][0] < cutoff:
            series.popleft()

    def latest(self, symbol: str) -> tuple[datetime, Decimal] | None:
        series = self._series.get(symbol)
        return series[-1] if series else None

    def source_of(self, symbol: str) -> str | None:
        return self._source.get(symbol)

    def window_of(self, symbol: str) -> int | None:
        return self._windows.get(symbol)

    def value_at(
        self, symbol: str, when: datetime, *, tolerance: timedelta = STRIKE_TOLERANCE
    ) -> Decimal | None:
        """The value in force at ``when``, or ``None`` if the series does not cover it.

        Returns the **last observation at or before** ``when``, which is what "the price at
        the beginning of the range" means for a feed that carries a value forward until it
        changes. ``None`` when the nearest such observation is older than ``tolerance``, or
        when the series starts after ``when`` -- we were not watching, and the strike
        cannot be inferred from a later price.
        """
        series = self._series.get(symbol)
        if not series:
            return None

        stamps = [stamp for stamp, _ in series]
        index = bisect_left(stamps, when)
        if index < len(stamps) and stamps[index] == when:
            return series[index][1]
        if index == 0:
            return None  # the series begins after the instant asked about

        stamp, value = series[index - 1]
        # A carried-forward value is valid until the next change, so an older observation
        # still answers -- *provided* a later one exists to prove the feed was live across
        # the instant. Without that proof the tolerance applies, because the alternative is
        # treating a feed that died before the window opened as though it had reported.
        if index < len(stamps):
            return value
        return value if when - stamp <= tolerance else None

    def volatility_per_second(self, symbol: str) -> Decimal | None:
        """Realized volatility of log returns, per second, or ``None`` when unmeasurable.

        Estimated from **non-overlapping** returns at a lag of at least
        ``MIN_LAG_WINDOWS`` times the averaging window. Non-overlapping because
        overlapping returns share observations and their sample variance understates the
        true one; long-lagged because a short-lag return of a moving average measures the
        average, not the market.

        Returns ``None`` rather than zero on insufficient history. Zero volatility makes
        every probability 0 or 1, which is the single most dangerous output this engine
        could produce.
        """
        series = self._series.get(symbol)
        if not series or len(series) < 2:
            return None

        window = self._windows.get(symbol) or 30
        lag = timedelta(seconds=window * MIN_LAG_WINDOWS)

        samples: list[float] = []
        anchor_time, anchor_value = series[0]
        for stamp, value in series:
            if stamp - anchor_time < lag:
                continue
            elapsed = (stamp - anchor_time).total_seconds()
            try:
                ret = ln(float(value) / float(anchor_value))
            except (ValueError, ZeroDivisionError, InvalidOperation):
                return None
            # Scaled to a per-second standard deviation before averaging, so samples over
            # slightly different elapsed times stay comparable.
            #
            # The divisor is **not** sqrt(elapsed). For a moving average of a random walk
            # the variance of a change over a lag D >= w is sigma^2 (D - w/3), not
            # sigma^2 D: the averaging removes variance, and dividing by the calendar lag
            # therefore reports a volatility about 9% low at D = 2w. Small next to the
            # sampling error, and free to correct.
            effective = elapsed - window / 3
            if effective <= 0:
                continue
            samples.append(ret / sqrt(effective))
            anchor_time, anchor_value = stamp, value

        if len(samples) < MIN_VOL_SAMPLES:
            return None

        mean = sum(samples) / len(samples)
        variance = sum((sample - mean) ** 2 for sample in samples) / (len(samples) - 1)
        if variance <= 0:
            return None

        measured = Decimal(str(sqrt(variance)))
        floor = MIN_ANNUALISED_VOLATILITY / Decimal(str(sqrt(float(SECONDS_PER_YEAR))))
        if measured < floor:
            log.info(
                "reference.volatility_floored",
                symbol=symbol,
                measured=str(measured),
                floor=str(floor),
                samples=len(samples),
            )
            return floor
        return measured

    def first_observed(self, symbol: str) -> datetime | None:
        """When the series begins, or ``None`` if there is none.

        Public because "were we watching when this window opened?" is a question callers
        legitimately need to answer before asking for a strike, and because a verification
        script reaching into the deque to find out would be reading state no consumer is
        supposed to depend on.
        """
        series = self._series.get(symbol)
        return series[0][0] if series else None

    def samples(self, symbol: str) -> int:
        """Distinct observations held. Exposed for the journal and for abstention reasons."""
        return len(self._series.get(symbol, ()))
