"""Translation between SDK models and DeepFlow domain models.

The anti-corruption layer. Every SDK shape is converted exactly once, here.
Keeping the conversions in one module means an SDK upgrade surfaces as
failures in this file rather than as subtly wrong numbers in an engine.

Written against payloads observed live from ``polymarket-client`` 0.10.0 on
2026-09-12, recorded in ``tests/fixtures/polymarket_payloads.json``. Two things
the field names alone do not tell you, both load-bearing:

1. **Book levels arrive worst-price-first on both sides.** ``bids`` ascend and
   ``asks`` descend, so the top of book is the *last* element of each tuple.
   :class:`~deepflow.core.domain.OrderBook` is the other way round, so both
   sides are reversed here. See :func:`to_order_book`.
2. **The resolution rules live in ``description``**, not in the ``resolution``
   group -- that holds UMA plumbing, and its ``source`` field was empty on
   every market sampled.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from deepflow.adapters.polymarket.sports_feed import SportsFeedEvent
from deepflow.core.domain import (
    BookLevel,
    FeeSchedule,
    Market,
    OrderBook,
    Outcome,
    PublicTrade,
)
from deepflow.core.enums import OrderSide, OutcomeSide
from deepflow.core.types import ClobTokenId, ConditionId, EventId


def _fee_schedule(sdk_schedule: Any) -> FeeSchedule | None:
    if sdk_schedule is None:
        return None
    return FeeSchedule(
        rate=Decimal(str(sdk_schedule.rate)),
        exponent=Decimal(str(sdk_schedule.exponent)),
        taker_only=bool(sdk_schedule.taker_only),
        rebate_rate=Decimal(str(sdk_schedule.rebate_rate)),
    )


def _tag_attr(sdk_tag: Any, attribute: str) -> str:
    """Read one field off a venue tag.

    Tags arrive as ``MarketTag(id, slug, label)`` objects, not strings. Coercing
    the object with ``str()`` -- which the earlier version did -- produced the
    repr, so every tag looked unique and none ever matched.
    """
    return str(getattr(sdk_tag, attribute, "") or "")


def _outcomes(sdk_outcomes: Any) -> tuple[Outcome, ...]:
    """Flatten the SDK's ``{yes, no}`` pair into our ordered tuple.

    A token id is ``None`` until the market's book opens, and such an outcome is
    dropped rather than carried as a placeholder: everything downstream keys on
    the token id, and a ``None`` there would fail much further from the cause.
    """
    outcomes: list[Outcome] = []
    for side, sdk_outcome in (
        (OutcomeSide.YES, sdk_outcomes.yes),
        (OutcomeSide.NO, sdk_outcomes.no),
    ):
        if sdk_outcome is None or sdk_outcome.token_id is None:
            continue
        outcomes.append(
            Outcome(
                token_id=ClobTokenId(str(sdk_outcome.token_id)),
                label=sdk_outcome.label,
                side=side,
            )
        )
    return tuple(outcomes)


def to_market(sdk_market: Any) -> Market:
    """Normalize an SDK market into :class:`Market`.

    The SDK groups its fields (``state``, ``trading``, ``outcomes``, ``sports``,
    ``resolution``) where older ``py-clob-client`` examples had them flat, so
    every access below is deliberate rather than copied.
    """
    state = sdk_market.state
    trading = sdk_market.trading
    sports = getattr(sdk_market, "sports", None)
    events = getattr(sdk_market, "events", None) or ()

    return Market(
        condition_id=ConditionId(str(sdk_market.condition_id)),
        event_id=EventId(str(events[0].id)) if events else None,
        question=sdk_market.question,
        slug=sdk_market.slug,
        outcomes=_outcomes(sdk_market.outcomes),
        active=bool(state.active),
        closed=bool(state.closed),
        accepting_orders=bool(state.accepting_orders),
        start_date=state.start_date,
        end_date=state.end_date,
        tags=tuple(_tag_attr(t, "slug") for t in (sdk_market.tags or ())),
        tag_ids=tuple(_tag_attr(t, "id") for t in (sdk_market.tags or ())),
        # The rules text is ``description``. ``resolution.source`` is UMA
        # plumbing and was empty on every market sampled, so it is recorded only
        # when actually populated rather than written in as an empty string that
        # would read as "checked, no source".
        resolution_source=(getattr(sdk_market.resolution, "source", "") or None),
        resolution_text=sdk_market.description,
        minimum_tick_size=trading.minimum_tick_size,
        minimum_order_size=trading.minimum_order_size,
        negative_risk=bool(state.neg_risk),
        enable_order_book=bool(state.enable_order_book),
        fee_type=getattr(trading, "fee_type", None),
        fees_enabled=bool(trading.fees_enabled),
        fee_schedule=_fee_schedule(trading.fee_schedule),
        # ``seconds_delay`` is ``None`` on an undelayed market, and ``None`` must
        # not become a truthy delay or a silent zero further down -- zero is the
        # correct reading, but only because the venue means "no delay".
        seconds_delay=int(trading.seconds_delay or 0),
        game_start_time=getattr(sports, "game_start_time", None),
        sports_market_type=getattr(sports, "sports_market_type", None),
    )


def to_order_book(sdk_book: Any) -> OrderBook:
    """Normalize an SDK order book.

    **Reverses both sides.** The venue sends each side worst-price-first: bids
    ascend from 0.001 and asks descend from 0.999, so the tradeable top of book
    is the last element of each tuple. :class:`OrderBook` expects bids descending
    and asks ascending, with the best price at index 0.

    Passing the wire order through unchanged is not caught by the crossed-book
    validator -- ``bids[0]=0.001`` against ``asks[0]=0.999`` is not crossed, it
    is merely absurd -- and yields a 99.8c spread on every market. The benign
    outcome is that every spread gate rejects and the system never trades. The
    dangerous one is a book walked from the wrong end, pricing a 0.04 contract's
    fill at 0.999.
    """
    bids = tuple(BookLevel(price=level.price, size=level.size) for level in reversed(sdk_book.bids))
    asks = tuple(BookLevel(price=level.price, size=level.size) for level in reversed(sdk_book.asks))
    return OrderBook(
        token_id=ClobTokenId(str(sdk_book.asset_id)),
        bids=bids,
        asks=asks,
        captured_at=sdk_book.timestamp,
    )


def to_public_trade(sdk_trade: Any) -> PublicTrade:
    """Normalize a Data API trade.

    ``asset_id`` is the outcome token; ``side`` is the *taker's* side as a plain
    string. The exact price is preserved rather than rounded -- a whale filling
    at 0.887 tells a different story from one filling at 0.91.
    """
    return PublicTrade(
        token_id=ClobTokenId(str(sdk_trade.asset_id)),
        price=sdk_trade.price,
        size=sdk_trade.size,
        side=OrderSide(str(sdk_trade.side).upper()),
        traded_at=sdk_trade.timestamp,
    )


def to_sports_feed_event(sdk_event: Any) -> SportsFeedEvent:
    """Normalize a ``sport_result`` payload.

    ``score`` arrives as one ``"<home>-<away>"`` string rather than two fields,
    and an unparseable or absent score must stay ``None`` rather than becoming
    0-0 -- see :meth:`SportsFeedEvent.scores`.
    """
    payload = getattr(sdk_event, "payload", sdk_event)
    return SportsFeedEvent.model_validate(payload, from_attributes=True)
