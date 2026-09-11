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

from decimal import Decimal

from deepflow.config.thresholds import Btc5mThresholds
from deepflow.core.domain import Market, MarketSnapshot, ProbabilityEstimate
from deepflow.core.enums import MarketCategory
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId
from deepflow.engines.base import BaseProbabilityEngine

log = get_logger(__name__)


class Btc5mEngine(BaseProbabilityEngine):
    """Barrier-crossing probability for short-dated BTC strike markets."""

    name = "btc_5m"
    categories = frozenset({MarketCategory.BTC_5M})

    def __init__(self, thresholds: Btc5mThresholds) -> None:
        self._thresholds = thresholds

    async def estimate(
        self,
        *,
        market: Market,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> ProbabilityEstimate | None:
        """Estimate P(settles above strike).

        TODO(skeleton):
        * read strike, reference source and settlement convention from the
          resolution criteria; abstain on any mismatch with the feed we hold
        * distance = (spot - strike) in volatility units
        * sigma from realized short-horizon volatility, not a fixed constant --
          BTC's 5-minute vol moves by multiples across a session
        * P from the barrier/terminal distribution at T
        * widen ``uncertainty`` as T shrinks: model error and reference-price
          error both grow exactly when the probability looks most extreme
        * abstain on a stale reference tick, however fresh the order book is

        Supporting inputs where available: futures/perp basis, funding, and
        liquidation clusters near the strike -- these change the short-horizon
        distribution's shape, which a spot-only model cannot see.
        """
        raise NotImplementedError("Btc5mEngine.estimate")

    def _short_horizon_volatility(self, snapshot: MarketSnapshot) -> Decimal | None:
        """Realized volatility over the relevant horizon."""
        raise NotImplementedError("Btc5mEngine._short_horizon_volatility")
