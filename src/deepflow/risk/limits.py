"""Risk engine. Section 17.

Applies the limit set and issues the final RISK_APPROVED verdict. Sizing and
exposure are separate modules; this one composes them and owns the daily-loss
and drawdown state that spans trades.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from deepflow.config.thresholds import RiskLimits
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId
from deepflow.risk.exposure import ExposureTracker
from deepflow.risk.sizing import SizingResult, size_position

log = get_logger(__name__)

#: Smallest stake worth submitting, in collateral.
#:
#: The venue's own floor is a per-market ``minimum_order_size`` (5 on the markets
#: sampled), and this sits above it deliberately: an order at exactly the minimum
#: pays the taker fee on a position too small for its edge to cover, so the binding
#: constraint should be "not worth it" rather than "not allowed".
MIN_MEANINGFUL_STAKE_USDC: Final = Decimal(10)


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

        Six independent vetoes, in a fixed order. Each can only refuse; none can
        rescue a trade an earlier one rejected, which is why they are evaluated
        narrowest-consequence-first: the portfolio-level stops come before sizing,
        because a system in drawdown should not be computing position sizes at all.

        The order also determines which reason reaches the journal, and the
        portfolio stops earn their place at the front for that reason too. "Daily
        loss limit breached" explains a whole day of refusals; "stake below dust"
        explains one, and reading the second when the first is also true sends you
        looking at the wrong thing.

        A rejection always carries a ``sizing`` result when one was computed, even
        though the trade is refused. The stake that *would* have been taken is what
        tells you whether a limit is binding meaningfully or strangling everything,
        and discarding it on rejection throws away the only record of that.
        """
        bankroll = self._bankroll

        # 1. Daily loss. Realized only: an unrealized loss is a position to manage,
        #    not a day to stop trading over.
        if bankroll.balance_usdc > 0:
            daily_loss_fraction = max(
                Decimal(0), -bankroll.realized_pnl_today / bankroll.balance_usdc
            )
            if daily_loss_fraction >= self._limits.max_daily_loss_fraction:
                return self._reject(
                    f"daily loss {daily_loss_fraction:.3f} at limit "
                    f"{self._limits.max_daily_loss_fraction}"
                )

        # 2. Drawdown from peak. Measured against the peak rather than the start, so
        #    a recovered account is not permanently barred by an old low.
        if bankroll.drawdown_fraction >= self._limits.max_drawdown_fraction:
            return self._reject(
                f"drawdown {bankroll.drawdown_fraction:.3f} at limit "
                f"{self._limits.max_drawdown_fraction}"
            )

        # 3. Position count, before sizing: at the cap, the size is irrelevant.
        open_count = self._exposure.snapshot.open_position_count
        if open_count >= self._limits.max_open_positions:
            return self._reject(
                f"open positions {open_count} at cap {self._limits.max_open_positions}"
            )

        # 4. Size it. ``size_position`` only ever shrinks, and names the binding cap.
        if not (0 < price < 1):
            return self._reject(f"price {price} outside (0, 1)")
        sizing = size_position(
            probability=probability,
            price=price,
            uncertainty=uncertainty,
            bankroll=bankroll.balance_usdc,
            limits=self._limits,
            available_capital=self.available_capital(),
        )

        # 5. Dust. A stake below the venue's minimum cannot be submitted, and one
        #    just above it pays a fee on a position too small to matter.
        if sizing.stake_usdc <= 0:
            return self._reject(f"sized to zero ({sizing.binding_constraint})", sizing)
        if sizing.stake_usdc < MIN_MEANINGFUL_STAKE_USDC:
            return self._reject(
                f"stake {sizing.stake_usdc} below minimum {MIN_MEANINGFUL_STAKE_USDC} "
                f"({sizing.binding_constraint})",
                sizing,
            )

        # 6. Exposure, across every dimension the limits name. Last because it is
        #    the only veto that needs the stake to evaluate.
        breach = self._exposure.would_exceed(
            stake_usdc=sizing.stake_usdc,
            condition_id=condition_id,
            bankroll=bankroll.balance_usdc,
            limits=self._limits,
        )
        if breach is not None:
            return self._reject(breach, sizing)

        log.info(
            "risk.approved",
            condition_id=str(condition_id),
            stake_usdc=str(sizing.stake_usdc),
            binding_constraint=sizing.binding_constraint,
        )
        return RiskVerdict(approved=True, sizing=sizing, reason=sizing.binding_constraint)

    @staticmethod
    def _reject(reason: str, sizing: SizingResult | None = None) -> RiskVerdict:
        log.info("risk.rejected", reason=reason)
        return RiskVerdict(approved=False, sizing=sizing, reason=reason)

    def update_bankroll(self, state: BankrollState) -> None:
        """Replace bankroll state wholesale.

        Wholesale rather than incremental, and for the same reason exposure is
        rebuilt rather than accumulated: balance and peak come from the venue and
        the ledger, and a locally maintained figure that drifted still looks like it
        is enforcing the drawdown limit.
        """
        self._bankroll = state

    @property
    def bankroll(self) -> BankrollState:
        return self._bankroll

    @property
    def limits(self) -> RiskLimits:
        return self._limits

    def replace_limits(self, limits: RiskLimits) -> None:
        """Swap the limits a subsequent decision will be judged against.

        Exists for the dashboard: restarting the process to change a position cap means
        losing the feed, the TWAP warm-up and every subscription, which is a real cost to
        pay for turning a number down during an incident.

        **It takes effect from the next decision and changes nothing already approved.**
        A tighter limit does not unwind an open position -- that is what the exit path is
        for -- and this is worth being explicit about, because an operator who tightens a
        limit mid-incident may believe they have just reduced their exposure.

        ``RiskLimits`` is frozen and validated, so an out-of-range value cannot arrive
        here; the caller builds the replacement with ``model_copy(update=...)`` and
        pydantic refuses anything the field constraints reject.
        """
        previous = self._limits
        self._limits = limits
        log.warning(
            "risk.limits_replaced",
            changed={
                field: f"{getattr(previous, field)} -> {getattr(limits, field)}"
                for field in type(limits).model_fields
                if getattr(previous, field) != getattr(limits, field)
            },
        )
