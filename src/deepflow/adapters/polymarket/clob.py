"""CLOB REST adapter -- books, midpoints, spreads, last trades.

Implements :class:`~deepflow.ports.market_data.MarketDataPort`.

Verified available on the SDK client (0.10.0): ``get_order_book``,
``get_order_books``, ``get_midpoint(s)``, ``get_price(s)``, ``get_spread(s)``,
``get_last_trade_price(s)``, ``estimate_market_price``.

The book response is also where the authoritative *trading constraints* live --
``tick_size``, ``min_order_size`` and ``neg_risk`` come back alongside the
levels. ``neg_risk`` in particular is a signing input (it selects the EIP-712
verifying contract), so it is read from here rather than inferred.

REST is the cross-check, not the primary feed. The stream drives trading; these
calls prime state on startup and re-anchor it after a reconnect, because a
stream that reconnected may have missed the update that moved the book.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from deepflow.adapters.polymarket import mapping
from deepflow.adapters.polymarket.sdk_client import PolymarketSession
from deepflow.core.domain import OrderBook, PublicTrade
from deepflow.core.errors import PolymarketApiError
from deepflow.core.logging import get_logger
from deepflow.core.types import ClobTokenId

log = get_logger(__name__)


class ClobMarketData:
    """Point-in-time CLOB reads."""

    def __init__(self, session: PolymarketSession) -> None:
        self._session = session

    async def get_order_book(self, token_id: ClobTokenId) -> OrderBook:
        sdk_book = await self._session.public.get_order_book(token_id=token_id)
        return mapping.to_order_book(sdk_book)

    async def get_order_books(self, token_ids: Sequence[ClobTokenId]) -> Sequence[OrderBook]:
        """Batched. Prefer this over N single calls -- it is one round trip and
        the snapshots share a timestamp, so derived spreads stay coherent.

        **The venue does not return books in request order**, and the ordering is
        not even stable between identical calls (observed differing on 5 of 5
        trials with 12 tokens, at varying positions). Results are therefore keyed
        by ``asset_id`` and re-projected onto the requested order.

        Zipping positionally would be catastrophic rather than merely wrong: a
        binary market's YES and NO books are near mirror images, so the failure
        mode is pricing a 0.04 outcome off its complement's 0.96 book. The engine
        sees a confident number that happens to be the reflection of the truth,
        and every gate downstream agrees with it.
        """
        if not token_ids:
            return ()

        sdk_books = await self._session.public.get_order_books(token_ids=list(token_ids))
        by_token = {str(book.asset_id): book for book in sdk_books}

        missing = [t for t in token_ids if t not in by_token]
        if missing:
            # Silently dropping these would leave the caller with a shorter
            # sequence than it asked for and no way to tell which token is
            # absent, which invites exactly the positional reasoning this method
            # exists to prevent.
            raise PolymarketApiError(
                f"get_order_books returned no book for {len(missing)} of "
                f"{len(token_ids)} tokens: {missing[:3]}"
            )

        return tuple(mapping.to_order_book(by_token[t]) for t in token_ids)

    async def get_midpoint(self, token_id: ClobTokenId) -> Decimal:
        """Midpoint price. Returns a bare ``Decimal``, not a wrapper object.

        Used for monitoring and display only. Never for EV: the mid is not a
        price anyone will trade with us, and mid-based edge is optimistic by half
        the spread on every trade.
        """
        midpoint = await self._session.public.get_midpoint(token_id=token_id)
        return Decimal(str(midpoint))

    async def get_spread(self, token_id: ClobTokenId) -> Decimal:
        spread = await self._session.public.get_spread(token_id=token_id)
        return Decimal(str(spread))

    async def get_last_trades(
        self, token_id: ClobTokenId, *, limit: int = 50
    ) -> Sequence[PublicTrade]:
        """Recent public trades for one outcome token.

        The Data API filters by ``condition_id``, not by token, so the market's
        trades are fetched and then narrowed to this token. Both outcomes of a
        binary market share a condition id, and half the returned trades are the
        other side.
        """
        book = await self._session.public.get_order_book(token_id=token_id)
        pages = self._session.public.list_trades(
            condition_id=str(book.condition_id), page_size=min(limit * 2, 100)
        )

        trades: list[PublicTrade] = []
        async for sdk_trade in pages.iter_items():
            if str(sdk_trade.asset_id) != str(token_id):
                continue
            trades.append(mapping.to_public_trade(sdk_trade))
            if len(trades) >= limit:
                break
        return tuple(trades)

    async def estimate_fill_price(
        self, token_id: ClobTokenId, *, size_shares: Decimal
    ) -> Decimal | None:
        """Walk the book for an expected fill price.

        Backs the slippage term in the EV calculation. The SDK exposes
        ``estimate_market_price`` for this; the local book walk is kept as a
        cross-check, since disagreement between the two is itself a signal that
        our book snapshot is stale.

        Walks the asks from the best price outward and returns the size-weighted
        average. ``None`` when the book cannot fill ``size_shares`` at all --
        distinct from a poor price, and the caller must not treat a partial walk
        as a fill estimate.
        """
        book = await self.get_order_book(token_id)

        remaining = Decimal(size_shares)
        cost = Decimal(0)
        for level in book.asks:  # best first, guaranteed by OrderBook's validator
            take = min(remaining, level.size)
            cost += take * level.price
            remaining -= take
            if remaining <= 0:
                return cost / Decimal(size_shares)

        log.warning(
            "clob.book_too_thin",
            token_id=token_id,
            requested=str(size_shares),
            short_by=str(remaining),
        )
        return None
