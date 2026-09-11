"""Risk engine. Section 17.

Applies the limit set and issues the final RISK_APPROVED verdict. Sizing and
exposure are separate modules; this one composes them and owns the daily-loss
and drawdown state that spans trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from deepflow.config.thresholds import RiskLimits
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId
from deepflow.risk.exposure import ExposureTracker
from deepflow.risk.sizing import SizingResult

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class RiskVerdict:
    approved: bool
    sizing: SizingResult | None
    reason: str


@dataclass(slots=True)
class BankrollState:
    """Capital state driving the portfolio-level limits."""

    balance_usdc: Decimal = Decimal(0)
    peak_balance_usdc: Decimal = Decimal(0)
    realized_pnl_today: Decimal = Decimal(0)

    @property
    def drawdown_fraction(self) -> Decimal:
        if self.peak_balance_usdc <= 0:
            return Decimal(0)
        return max(
            Decimal(0),
            (self.peak_balance_usdc - self.balance_usdc) / self.peak_balance_usdc,
        )


class RiskEngine:
    """Final risk authority before execution."""

    def __init__(self, limits: RiskLimits, exposure: ExposureTracker) -> None:
        self._limits = limits
        self._exposure = exposure
        self._bankroll = BankrollState()

    def available_capital(self) -> Decimal:
        """Deployable capital, after the untouchable reserve.

        The reserve exists so an emergency exit or a settlement lag never finds
        the account fully committed -- the moment capital is most needed is the
        moment everything else has already gone wrong.
        """
        reserve = self._bankroll.balance_usdc * self._limits.reserved_capital_fraction
        return max(Decimal(0), self._bankroll.balance_usdc - reserve)

    def approve(
        self,
        *,
        probability: Decimal,
        price: Decimal,
        uncertainty: Decimal,
        condition_id: ConditionId,
    ) -> RiskVerdict:
        """Size the trade and decide.

        TODO(skeleton), in order -- each is an independent veto:
        1. daily loss limit breached -> reject
        2. drawdown limit breached -> reject
        3. open position count at max -> reject
        4. size via ``risk.sizing.size_position``
        5. zero or dust size -> reject
        6. exposure check across event, correlation group, strategy, total
        """
        raise NotImplementedError("RiskEngine.approve")

    @property
    def bankroll(self) -> BankrollState:
        return self._bankroll
