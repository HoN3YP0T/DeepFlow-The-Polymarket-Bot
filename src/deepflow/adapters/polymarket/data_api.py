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

from collections.abc import Awaitable, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, TypeVar

from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.core.domain import SmartMoneyEntry, WalletHolding, WalletPerformance
from deepflow.core.enums import OrderSide
from deepflow.core.errors import PolymarketApiError
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId, ConditionId, WalletAddress

log = get_logger(__name__)

T = TypeVar("T")

#: Venue page cap. Documented maxima are unreliable (§65), so requests are sized to the
#: measured ceiling rather than the published one.
_MAX_PAGE: Final = 100

#: The holder list applies its page size **per outcome**, so this is a per-side cap.
#: ``include_pnl=True`` additionally caps it at 100.
_MAX_HOLDER_PAGE: Final = 100


async def _quiet(awaitable: Awaitable[T], call: str, wallet: str) -> T | None:
    """Await a lookup, downgrading a failure to ``None`` and a log line.

    Used only for the wallet-performance composition, where each field stands alone: one
    unavailable endpoint should lower a wallet's score, not suppress smart-money
    analysis for the whole market.
    """
    try:
        return await awaitable
    except Exception as exc:
        log.warning("data_api.lookup_failed", call=call, wallet=wallet, error=str(exc))
        return None


def _to_entry(trade: Any) -> SmartMoneyEntry:
    """One trade as a wallet-attributed action.

    ``market_probability_at_entry`` is the executed price. For a **taker** those are the
    same number -- the price they paid *was* the market's probability at that instant --
    and the Data API offers no book snapshot at the trade's timestamp to say otherwise.
    Recorded explicitly so nobody later reads it as an independently-sourced mark.

    ``is_exit`` is read from the side, and that is a real limitation rather than a
    shortcut: on a binary outcome token, selling the token you hold is the only way to
    reduce, but *buying the complement* is also a way to bet against it and appears as a
    BUY of the other token. From trades alone the two are indistinguishable, so a
    wallet hedging via the complement reads as a new entry. ``is_increase`` needs the
    wallet's prior position and is therefore left ``False`` here -- the holder list is
    where that question is answerable.
    """
    price = Decimal(str(trade.price))
    return SmartMoneyEntry(
        wallet=WalletAddress(str(trade.wallet)),
        side=OrderSide(str(trade.side).upper()),
        outcome_label=str(getattr(trade, "outcome", None) or ""),
        notional_usdc=Decimal(str(trade.size)) * price,
        entry_price=price,
        market_probability_at_entry=price,
        observed_at=trade.timestamp,
        is_exit=OrderSide(str(trade.side).upper()) is OrderSide.SELL,
    )


def _to_holding(holder: Any, *, fallback_token: object = "") -> WalletHolding:
    """One holder as a holding. ``amount`` is shares, per the venue's own docstring."""
    token = getattr(holder, "asset_id", None) or fallback_token
    return WalletHolding(
        wallet=WalletAddress(str(holder.wallet)),
        token_id=ClobTokenId(str(token)),
        shares=Decimal(str(getattr(holder, "amount", 0) or 0)),
        average_entry_price=_decimal_or_none(getattr(holder, "avg_price", None)),
        realized_pnl=_decimal_or_none(getattr(holder, "realized_pnl", None)),
        unrealized_pnl=_decimal_or_none(getattr(holder, "unrealized_pnl", None)),
    )


def _latest_realized_pnl(series: Any) -> Decimal | None:
    """Cumulative realized PnL from the last point of the series.

    The series is cumulative, so the final point is the answer and summing the points
    would multiply it. ``realized_pnl`` rather than total PnL: unrealized movement on
    open positions is an opinion the wallet has not yet been proven right about.
    """
    points = tuple(getattr(series, "points", ()) or ())
    if not points:
        return None
    return _decimal_or_none(getattr(points[-1], "realized_pnl", None))


def _portfolio_value(value: Any) -> Decimal | None:
    """Total portfolio value, however the venue happens to name the field."""
    if value is None:
        return None
    for attribute in ("value", "portfolio_value", "total_value"):
        found = _decimal_or_none(getattr(value, attribute, None))
        if found is not None:
            return found
    return _decimal_or_none(value) if isinstance(value, int | float | Decimal | str) else None


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return None


def _int_or_none(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(Decimal(str(value)))
    except (ArithmeticError, TypeError, ValueError):
        return None


class DataApiWalletIntel:
    """Trader activity and performance lookups."""

    def __init__(self, session: PolymarketSession) -> None:
        self._session = session

    async def list_market_trades(
        self, condition_id: ConditionId, *, since: datetime | None = None, limit: int = 200
    ) -> Sequence[SmartMoneyEntry]:
        """Recent trades on a market, as wallet-attributed entries.

        The exact execution price is preserved rather than rounded: the dashboard shows
        the odds a whale actually paid, and a whale filling at 0.887 tells a different
        story from one filling at 0.91.

        ``taker_only=True`` because a taker chose to trade *now*, at the price on
        offer. A maker's fill says only that someone else crossed into their resting
        order, which carries no information about the maker's timing -- and timing is
        one of the things the score is built from.
        """
        client = self._session.public
        try:
            page = await client.list_trades(
                condition_id=str(condition_id),
                taker_only=True,
                start=since,
                page_size=min(limit, _MAX_PAGE),
            ).first_page()
        except Exception as exc:
            raise PolymarketApiError(f"list_market_trades {condition_id}: {exc}") from exc
        return tuple(_to_entry(trade) for trade in page.items[:limit])

    async def list_market_holders(
        self, condition_id: ConditionId, *, limit: int = 100
    ) -> Sequence[WalletHolding]:
        """Current holders, flattened across outcomes.

        The venue groups holders **by outcome asset** and applies the page size *per
        outcome*, so a binary market returns two groups. Flattened here because the
        caller wants "who holds what", and the grouping is a transport detail -- but the
        token id travels on every holding, because which side a wallet is on is the
        whole point.
        """
        client = self._session.public
        try:
            page = await client.list_market_holders(
                condition_ids=str(condition_id),
                include_pnl=True,
                page_size=min(limit, _MAX_HOLDER_PAGE),
            ).first_page()
        except Exception as exc:
            raise PolymarketApiError(f"list_market_holders {condition_id}: {exc}") from exc

        holdings: list[WalletHolding] = []
        for group in page.items:
            for holder in getattr(group, "holders", ()) or ():
                holdings.append(_to_holding(holder, fallback_token=getattr(group, "asset_id", "")))
        return tuple(holdings[:limit])

    async def get_wallet_performance(
        self, wallet: WalletAddress, *, since: datetime | None = None
    ) -> WalletPerformance | None:
        """Compose one performance record from the several calls that answer parts of it.

        The Data API splits this across endpoints -- stats, a PnL series, volume, closed
        positions, portfolio value -- and none of them alone is enough to score a wallet.
        Composed here rather than in the engine so the engine depends on one shape
        instead of five endpoints.

        **A failing call degrades the record rather than failing the lookup.** Each
        field is independently optional and `None` means unmeasured, so a wallet whose
        PnL series is unavailable is still scored on what did come back -- and scored
        *lower* for it, because unproven is the correct reading of missing evidence.
        Raising instead would make one flaky endpoint suppress smart-money analysis
        entirely.
        """
        client = self._session.public
        address = str(wallet)

        stats = await _quiet(client.get_user_stats(user=address), "get_user_stats", address)
        pnl = await _quiet(client.get_user_pnl(user=address), "get_user_pnl", address)
        volume = await _quiet(
            client.get_user_volume(user=address, start=since), "get_user_volume", address
        )
        closed, winning = await self._closed_record(address)
        portfolio = await _quiet(
            client.get_portfolio_value(user=address), "get_portfolio_value", address
        )

        if stats is None and pnl is None and volume is None and closed is None:
            # Nothing at all came back. Distinct from a wallet with a measured record of
            # zero: this one is unproven, and the scorer must be able to tell.
            return None

        return WalletPerformance(
            wallet=wallet,
            realized_pnl_usdc=_latest_realized_pnl(pnl),
            volume_usdc=_decimal_or_none(getattr(volume, "volume_usdc", None)),
            trade_count=_int_or_none(getattr(volume, "trade_count", None)),
            markets_traded=_int_or_none(getattr(stats, "traded_market_count", None)),
            first_seen_at=getattr(stats, "join_date", None),
            portfolio_value_usdc=_portfolio_value(portfolio),
            closed_positions=closed,
            winning_positions=winning,
        )

    async def _closed_record(self, address: str) -> tuple[int | None, int | None]:
        """Resolved positions and how many of them made money.

        A hit rate on *resolved* markets, which is a different and far better question
        than whether the wallet is currently up on open bets -- an unresolved position is
        an opinion, not a result.

        **Sorted by timestamp, explicitly** (finding 75). The endpoint's default order is
        realized PnL **descending**, so one page of a prolific wallet is its 100 *best*
        positions: measured live, a wallet whose cumulative realized PnL is -964 returned
        100 of 100 winners summing to +3,861, and every wallet with 100 or more closed
        positions would have scored a perfect hit rate. Sorting by time does not make the
        sample random, but it makes it **unbiased with respect to the thing being
        measured**, which is what the rate needs.

        Still capped at one page. A hit rate over the most recent 100 resolved positions
        is the honest reading; paging a five-thousand-position wallet would cost fifty
        requests to answer a question that recency answers well enough.
        """
        try:
            page = await self._session.public.list_positions(
                user=address,
                status="CLOSED",
                sort_by="TIMESTAMP",
                sort_direction="DESC",
                page_size=_MAX_PAGE,
            ).first_page()
        except Exception as exc:
            log.warning("data_api.closed_positions_unavailable", wallet=address, error=str(exc))
            return None, None

        positions = tuple(page.items)
        if not positions:
            return 0, 0
        winning = sum(1 for p in positions if Decimal(str(getattr(p, "realized_pnl", 0) or 0)) > 0)
        return len(positions), winning
