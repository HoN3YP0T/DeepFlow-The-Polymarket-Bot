"""Wallet and trader intelligence port (Data API).

Facts only. Every method here answers "what happened"; turning that into a score is
judgement and lives in :mod:`deepflow.engines.smart_money`. The split matters because
the scoring rules will change often and the venue's data will not.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from deepflow.core.domain import SmartMoneyEntry, WalletHolding, WalletPerformance
from deepflow.core.types import ConditionId, WalletAddress


@runtime_checkable
class WalletIntelPort(Protocol):
    """Historical and live trader activity, used to score wallets.

    Scoring inputs come from realized performance over a lookback window -- never from
    position size in isolation.
    """

    async def list_market_trades(
        self, condition_id: ConditionId, *, since: datetime | None = None, limit: int = 200
    ) -> Sequence[SmartMoneyEntry]: ...

    async def list_market_holders(
        self, condition_id: ConditionId, *, limit: int = 100
    ) -> Sequence[WalletHolding]: ...

    async def get_wallet_performance(
        self, wallet: WalletAddress, *, since: datetime | None = None
    ) -> WalletPerformance | None:
        """Realized performance over the lookback window.

        ``None`` when the venue has nothing on the wallet, which is distinct from a
        wallet with a measured record of zero: the first is unproven and the second is
        proven mediocre, and a scorer must be able to tell them apart.
        """
        ...
