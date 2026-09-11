"""Data API adapter -- wallet and trader intelligence.

Implements :class:`~deepflow.ports.wallet_intel.WalletIntelPort`.

Confirmed on the SDK client (0.10.0): ``list_trades``, ``list_activity``,
``list_positions``, ``list_market_holders``, ``get_user_stats``,
``get_user_pnl``, ``get_user_volume``, ``list_trader_leaderboard``,
``get_trader_leaderboard_standing``, ``list_biggest_winners``,
``get_portfolio_value``, ``list_price_history``, ``get_open_interests``.

This adapter reads facts only. Turning wallet history into a score is a
modelling decision and lives in ``deepflow.engines.smart_money``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.core.domain import SmartMoneyEntry
from deepflow.core.logging import get_logger
from deepflow.core.types import ConditionId, WalletAddress

log = get_logger(__name__)


class DataApiWalletIntel:
    """Trader activity and performance lookups."""

    def __init__(self, session: PolymarketSession) -> None:
        self._session = session

    async def list_market_trades(
        self, condition_id: ConditionId, *, since: datetime | None = None, limit: int = 200
    ) -> Sequence[SmartMoneyEntry]:
        """Recent trades on a market, as wallet-attributed entries.

        The exact execution price is preserved rather than rounded: the
        dashboard shows the odds a whale actually paid, and a whale filling at
        0.887 tells a different story from one filling at 0.91.
        """
        raise NotImplementedError("DataApiWalletIntel.list_market_trades")

    async def list_market_holders(
        self, condition_id: ConditionId, *, limit: int = 100
    ) -> Sequence[object]:
        raise NotImplementedError("DataApiWalletIntel.list_market_holders")

    async def get_wallet_stats(self, wallet: WalletAddress) -> object | None:
        raise NotImplementedError("DataApiWalletIntel.get_wallet_stats")

    async def get_wallet_pnl(
        self, wallet: WalletAddress, *, since: datetime | None = None
    ) -> object | None:
        raise NotImplementedError("DataApiWalletIntel.get_wallet_pnl")

    async def list_wallet_positions(self, wallet: WalletAddress) -> Sequence[object]:
        raise NotImplementedError("DataApiWalletIntel.list_wallet_positions")
