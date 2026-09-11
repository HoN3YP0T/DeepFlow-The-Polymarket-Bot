"""Translation between SDK models and DeepFlow domain models.

The anti-corruption layer. Every SDK shape is converted exactly once, here.
Keeping the conversions in one module means an SDK upgrade surfaces as
failures in this file rather than as subtly wrong numbers in an engine.
"""

from __future__ import annotations

from typing import Any

from deepflow.core.domain import Market, OrderBook, PublicTrade


def to_market(sdk_market: Any) -> Market:
    """Normalize an SDK market into :class:`Market`.

    TODO(skeleton): map condition id, outcomes/token ids, status flags, end
    date, tick size and the resolution text. Field names must be read off the
    installed SDK models rather than assumed.
    """
    raise NotImplementedError("mapping.to_market")


def to_order_book(sdk_book: Any) -> OrderBook:
    """Normalize an SDK order book.

    Must sort bids descending and asks ascending -- ``OrderBook`` validates
    that the book is not crossed and relies on level ordering for best-price.
    """
    raise NotImplementedError("mapping.to_order_book")


def to_public_trade(sdk_trade: Any) -> PublicTrade:
    raise NotImplementedError("mapping.to_public_trade")
