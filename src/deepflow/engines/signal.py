"""Signal engine. Sections 7-8, 16.

Assembles a trade intent from the independent pieces, and applies the
conjunctive entry conditions the brief specifies. Every condition must hold:

    market probability in the candidate band
  + an independent model confirms it
  + game/event state supports the outcome
  + positive net EV
  + sufficient liquidity
  + acceptable spread and slippage
  + fresh data
  + valid resolution
  + risk approval

The candidate band is the cheapest of these and the most dangerous to mistake
for a signal. Being priced at 94% is a reason to *look*, never a reason to buy:
that price is the market's estimate, and buying it because it is high is
betting that high probabilities are underpriced, which is the opposite of what
the favourite-longshot bias in prediction markets suggests.
"""

from __future__ import annotations

from deepflow.config.thresholds import Thresholds
from deepflow.core.domain import (
    Classification,
    Market,
    MarketSnapshot,
    ResolutionCriteria,
    Signal,
)
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId
from deepflow.engines.ev import EvEngine
from deepflow.engines.microstructure import MicrostructureEngine
from deepflow.engines.registry import EngineRegistry
from deepflow.engines.smart_money import SmartMoneyEngine

log = get_logger(__name__)


class SignalEngine:
    """Produces trade signals, or documented rejections."""

    def __init__(
        self,
        *,
        registry: EngineRegistry,
        ev: EvEngine,
        flow: MicrostructureEngine,
        smart_money: SmartMoneyEngine,
        thresholds: Thresholds,
    ) -> None:
        self._registry = registry
        self._ev = ev
        self._flow = flow
        self._smart_money = smart_money
        self._thresholds = thresholds

    async def evaluate(
        self,
        *,
        market: Market,
        classification: Classification,
        resolution: ResolutionCriteria,
        snapshot: MarketSnapshot,
        token_id: ClobTokenId,
    ) -> Signal:
        """Evaluate one outcome.

        Always returns a :class:`Signal`; a rejection is a signal with action
        ``NO_TRADE`` and a populated ``rationale``. Returning ``None`` for
        rejections would make the rejected set invisible, and the rejected set
        is how the gates get calibrated.

        TODO(skeleton):
        1. resolve the engine for ``classification``; no engine -> NO_TRADE
        2. candidate band check -> NO_TRADE if outside
        3. ``engine.estimate(...)``; abstention -> NO_TRADE
        4. model must confirm by at least ``min_model_confirmation``
        5. flow assessment and liquidity/slippage gates
        6. smart-money signal as a capped weighted feature
        7. ``ev.assess(...)``; non-positive net EV -> NO_TRADE
        8. late-game window (section 8) applied here for clock sports
        """
        raise NotImplementedError("SignalEngine.evaluate")
