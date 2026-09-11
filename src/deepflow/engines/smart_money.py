"""Whale and smart-money engine. Sections 11-12.

Two distinct jobs, kept separate on purpose:

* **Detection** -- large trades, new positions, increases, reductions, exits,
  clusters of wallets entering the same side. This is observation.
* **Scoring** -- how much a given wallet's action should count. This is
  judgement, and it is earned from realized performance, ROI, category
  specialization, entry timing and position significance relative to that
  wallet's own book.

Size is a detection trigger, never a score. A wallet is not smart because it is
large; plenty of large wallets are consistently wrong, and following them is
worse than trading nothing because the sizing feels justified.

The output is a weighted feature into the probability and EV engines, capped by
``SmartMoneyThresholds.max_weight_in_signal``. It cannot, on its own, produce a
trade -- blind copying inherits their entry price, their horizon and their
hedges, none of which we can see.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from deepflow.config.thresholds import SmartMoneyThresholds
from deepflow.core.domain import SmartMoneyEntry, SmartMoneySignal
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId, WalletAddress
from deepflow.ports.wallet_intel import WalletIntelPort

log = get_logger(__name__)


class SmartMoneyEngine:
    """Detects notable wallet activity and scores it."""

    def __init__(self, intel: WalletIntelPort, thresholds: SmartMoneyThresholds) -> None:
        self._intel = intel
        self._thresholds = thresholds

    async def analyse(
        self, condition_id: ConditionId, *, since: datetime | None = None
    ) -> SmartMoneySignal:
        """Build the smart-money picture for one market.

        TODO(skeleton): pull recent trades and holders, group by wallet, split
        entries from exits, score each wallet, and aggregate into a signal.

        Exits are tracked as carefully as entries. A scored wallet unwinding a
        position we are about to enter is a stronger message than a new wallet
        entering, and the dashboard shows both.
        """
        raise NotImplementedError("SmartMoneyEngine.analyse")

    async def score_wallet(self, wallet: WalletAddress) -> int:
        """SMART_MONEY_SCORE, 0-100.

        TODO(skeleton): combine over the lookback window --
        * realized PnL and ROI (risk-adjusted, not gross)
        * directional accuracy on resolved markets
        * category specialization: a wallet excellent at politics earns no
          credit on a cricket market
        * entry timing: consistently early relative to the move, or late
        * position significance relative to that wallet's own portfolio -- a
          $50k position is a conviction bet for one wallet and a rounding error
          for another

        Explicitly *not* an input: absolute position size. That is what makes a
        whale, and whales are not automatically informed.

        Wallets with too little history score low by default. An unproven
        wallet is unproven, not average.
        """
        raise NotImplementedError("SmartMoneyEngine.score_wallet")

    def _is_notable(self, entry: SmartMoneyEntry) -> bool:
        """Whether an action clears the detection floor."""
        return entry.notional_usdc >= self._thresholds.min_notional_usdc

    def _aggregate_weight(self, entries: tuple[SmartMoneyEntry, ...]) -> Decimal:
        """Combined weight, hard-capped by ``max_weight_in_signal``."""
        raise NotImplementedError("SmartMoneyEngine._aggregate_weight")
