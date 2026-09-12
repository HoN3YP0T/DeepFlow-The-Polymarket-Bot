"""Translation between SDK models and DeepFlow domain models.

The anti-corruption layer. Every SDK shape is converted exactly once, here.
Keeping the conversions in one module means an SDK upgrade surfaces as
failures in this file rather than as subtly wrong numbers in an engine.
"""

from __future__ import annotations

from typing import Any

from deepflow.adapters.polymarket.sports_feed import SportsFeedEvent
from deepflow.core.domain import Market, OrderBook, PublicTrade


def to_market(sdk_market: Any) -> Market:
    """Normalize an SDK market into :class:`Market`.

    TODO(skeleton): field names must be read off the installed SDK models
    rather than assumed. The SDK groups them, which is easy to miss when
    porting from older ``py-clob-client`` examples where they were flat:

    * identifiers: ``market.condition_id`` (also ``market.id``, the Gamma id, and
      ``market.slug``) -- the condition id is what positions and analytics use
    * outcomes: ``market.outcomes.yes`` / ``.no``, each with ``token_id``,
      ``label``, ``price``. ``token_id`` is ``None`` until the book opens.
    * status: ``market.state.{active, closed, archived, accepting_orders,
      enable_order_book, neg_risk, start_date, end_date, closed_time}``
    * constraints: ``market.trading.{minimum_tick_size, minimum_order_size,
      seconds_delay, fees_enabled, fee_schedule}``
    * sports: ``market.sports.{game_start_time, sports_market_type}``

    ``minimum_order_size`` is a collateral notional, not a share count, and
    ``fee_schedule`` must be carried through -- dropping it makes every EV
    figure downstream optimistic by the taker fee.
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


def to_sports_feed_event(sdk_event: Any) -> SportsFeedEvent:
    """Normalize a ``sport_result`` payload.

    ``score`` arrives as one ``"<home>-<away>"`` string rather than two fields,
    and an unparseable or absent score must stay ``None`` rather than becoming
    0-0 -- see :meth:`SportsFeedEvent.scores`.
    """
    raise NotImplementedError("mapping.to_sports_feed_event")
