"""Wallet and trader intelligence port (Data API)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from deepflow.core.domain import SmartMoneyEntry
from deepflow.core.types import ConditionId, WalletAddress


@runtime_checkable
class WalletIntelPort(Protocol):
    """Historical and live trader activity, used to score wallets.

    Scoring inputs come from realized performance over a lookback window --
    never from position size in isolation.
    """

    async def list_market_trades(
        self, condition_id: ConditionId, *, since: datetime | None = None, limit: int = 200
    ) -> Sequence[SmartMoneyEntry]: ...

    async def list_market_holders(
        self, condition_id: ConditionId, *, limit: int = 100
    ) -> Sequence[object]: ...

    async def get_wallet_stats(self, wallet: WalletAddress) -> object | None:
        """Aggregate performance: volume, realized PnL, activity span."""
        ...

    async def get_wallet_pnl(
        self, wallet: WalletAddress, *, since: datetime | None = None
    ) -> object | None: ...

    async def list_wallet_positions(self, wallet: WalletAddress) -> Sequence[object]: ...
