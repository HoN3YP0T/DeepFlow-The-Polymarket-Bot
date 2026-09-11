"""Early exit engine. Section 9.

Continuously reevaluates every open position and returns one of HOLD, ADD,
PARTIAL_EXIT, FULL_EXIT or EMERGENCY_EXIT.

The engine's whole difficulty is one distinction: **noise versus state change.**

A position bought at 94% that ticks to 92% has not changed -- that is spread,
a single impatient seller, or a thin book being thin. Exiting there pays the
spread twice and converts a positive-EV trade into a realized loss, repeatedly.

A position bought at 94% where the underlying event has changed -- a goal back,
a red card, a wicket, a break of serve, a confirmed geopolitical development,
a BTC move through the strike -- is a different position, and the entry
probability is now irrelevant. That is where exits must be immediate and
aggressive.

So the design separates the two inputs. Price-derived signals (velocity,
opposing flow, smart-money exits) are treated as *evidence* and must clear a
noise band scaled to the market's own volatility. State-derived signals (a
verified event) bypass the band entirely and can trigger an emergency exit on
their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from deepflow.core.domain import (
    ExitDecision,
    MarketSnapshot,
    Position,
    ProbabilityEstimate,
    SmartMoneySignal,
)
from deepflow.core.enums import ExitAction
from deepflow.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ExitSignals:
    """Inputs to an exit decision.

    ``state_change_detected`` is deliberately distinct from every price-derived
    field. It is the only input permitted to trigger an emergency exit, because
    it is the only one that reflects the world rather than the order book.
    """

    current_probability: Decimal
    entry_probability: Decimal
    model_probability: Decimal | None
    probability_velocity: Decimal | None = None
    probability_acceleration: Decimal | None = None
    state_change_detected: bool = False
    state_change_detail: str = ""
    opposing_flow: Decimal | None = None
    smart_money_exiting: bool = False
    time_remaining_seconds: int | None = None
    current_net_ev: Decimal | None = None


class ExitEngine:
    """Decides what to do with an open position."""

    def __init__(self, *, noise_band: Decimal = Decimal("0.03")) -> None:
        self._noise_band = noise_band
        """Default probability move treated as noise. Scaled per market by the
        market's own realized volatility -- a fixed band is too tight on a
        volatile BTC market and far too loose on a settled football market."""

    async def evaluate(
        self,
        *,
        position: Position,
        snapshot: MarketSnapshot,
        estimate: ProbabilityEstimate | None,
        smart_money: SmartMoneySignal | None,
    ) -> ExitDecision:
        """Decide an action for ``position``.

        TODO(skeleton), in precedence order:

        1. **EMERGENCY_EXIT** -- a verified underlying state change that
           invalidates the thesis: a goal or red card against the position, a
           wicket that flips the chase, a break of serve reversing the match,
           a badminton reversal at match point, a confirmed geopolitical
           development, BTC crossing the strike. Immediate and full; a market
           order is justified here because not getting out is the bigger risk.
        2. **FULL_EXIT** -- the model no longer supports the position, or net
           EV has gone negative at the current price, with the move outside the
           noise band.
        3. **PARTIAL_EXIT** -- deterioration that is real but not decisive:
           reduce size, keep the thesis.
        4. **ADD** -- the thesis strengthened and the price improved. Gated by
           the same risk limits as a new entry; adding is a new trade.
        5. **HOLD** -- the default. Everything inside the noise band resolves
           here.

        Time remaining is a modifier throughout: close to resolution, a small
        adverse move is far more informative than the same move earlier, and
        the noise band must tighten accordingly.
        """
        raise NotImplementedError("ExitEngine.evaluate")

    def _is_noise(self, signals: ExitSignals, *, volatility: Decimal | None) -> bool:
        """Whether an adverse move is inside the market's own noise band."""
        raise NotImplementedError("ExitEngine._is_noise")

    @staticmethod
    def hold(position: Position, reason: str) -> ExitDecision:
        """The default decision."""
        return ExitDecision(
            position_id=position.position_id,
            action=ExitAction.HOLD,
            exit_score=0,
            reason=reason,
        )
