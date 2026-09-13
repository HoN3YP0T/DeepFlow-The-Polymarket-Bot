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

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from deepflow.adapters.polymarket.sports_feed import SportsFeedEvent
from deepflow.core.domain import (
    BookLevel,
    FeeSchedule,
    Market,
    OrderBook,
    OrderRecord,
    Outcome,
    Position,
    PublicTrade,
)
from deepflow.core.enums import OrderSide, OrderStatus, OutcomeSide
from deepflow.core.logging import get_logger
from deepflow.core.types import (
    ClientOrderKey,
    ClobTokenId,
    ConditionId,
    EventId,
    OrderId,
    PositionId,
)

log = get_logger(__name__)

#: Stand-in for a position the venue reports with no creation time.
#:
#: Epoch rather than "now": a position dated now would look freshly opened, and exit
#: rules that read holding age would treat a long-held position as new. An obviously
#: wrong date is safer than a plausibly wrong one.
_EPOCH: Final = datetime(1970, 1, 1, tzinfo=UTC)


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
        group_item_title=getattr(sdk_market, "group_item_title", None),
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


def with_event_context(market: Market, sdk_event: Any) -> Market:
    """Attach what only the parent event knows.

    Two fields a market cannot supply about itself:

    * ``event_id`` -- a market reached *through* an event has an empty ``events``
      list, because the venue does not repeat the parent inside the child.
    * ``event_start_time`` -- when the contest begins. The SDK's market model drops
      the venue's ``eventStartTime`` entirely, and the stub event nested on a market
      carries only id, slug and title. The event's ``schedule.start_time`` is the
      only reachable source.
    * ``tags`` -- **the venue tags the event, not the market.** Every market on a
      captured short-dated crypto event had ``tags=[]`` while its event carried
      ``crypto``, the asset, and a literal ``5M`` cadence tag. Since tags are the
      classifier's primary tier, a market reached through an event arrives with its
      single most authoritative signal missing unless it is inherited here.

    Shared by the sports and short-dated-crypto paths on purpose. Both of them
    exist because the same mistake was made twice: measuring a contest from
    ``start_date``, which is when the *market* opened. One helper means fixing it
    once.
    """
    schedule = getattr(sdk_event, "schedule", None)
    event_tags = tuple(getattr(sdk_event, "tags", None) or ())

    update: dict[str, Any] = {
        "event_id": EventId(str(sdk_event.id)),
        "event_start_time": getattr(schedule, "start_time", None),
    }
    # Inherited only when the market has none of its own. A market that carries
    # tags is the more specific statement and must win; an empty tuple on a market
    # means "not populated" as often as "untagged", which is precisely the case
    # this fills.
    if event_tags and not market.tag_ids:
        update["tags"] = tuple(_tag_attr(tag, "slug") for tag in event_tags)
        update["tag_ids"] = tuple(_tag_attr(tag, "id") for tag in event_tags)

    return market.model_copy(update=update)


#: Venue order status -> ours. The venue reports lowercase strings.
#:
#: ``delayed`` is the one that must not be collapsed: on a market with a matching
#: delay an accepted order returns zero fills and no trade ids, which reads as either
#: a rejection or an unfilled order unless it is carried through as its own state.
_ORDER_STATUS: Final[dict[str, OrderStatus]] = {
    "live": OrderStatus.OPEN,
    "open": OrderStatus.OPEN,
    "delayed": OrderStatus.DELAYED,
    "matched": OrderStatus.MATCHED_UNSETTLED,
    "partially_filled": OrderStatus.PARTIALLY_FILLED,
    "filled": OrderStatus.FILLED,
    "canceled": OrderStatus.CANCELLED,
    "cancelled": OrderStatus.CANCELLED,
    "unmatched": OrderStatus.CANCELLED,
    "rejected": OrderStatus.REJECTED,
}


def to_order_status(raw: object, *, filled: Decimal, size: Decimal) -> OrderStatus:
    """Map a venue status string, falling back to what the fills say.

    An **unrecognised** status becomes ``UNKNOWN`` rather than a guess. A new venue
    state read as OPEN would leave an order being polled forever; read as FILLED it
    would book a position that may not exist. ``UNKNOWN`` routes it to reconciliation,
    which is the only safe destination for a state we do not understand.

    A recognised ``OPEN`` is upgraded to ``PARTIALLY_FILLED`` when the venue reports
    a non-zero matched size, because "live with 40 of 100 matched" is a partial fill
    whatever the status field says.
    """
    status = _ORDER_STATUS.get(str(raw or "").strip().lower())
    if status is None:
        log.warning("mapping.unknown_order_status", status=str(raw))
        return OrderStatus.UNKNOWN
    if status is OrderStatus.OPEN and filled > 0:
        return OrderStatus.FILLED if filled >= size > 0 else OrderStatus.PARTIALLY_FILLED
    return status


def to_order_record(sdk_order: Any, *, client_key: ClientOrderKey | None = None) -> OrderRecord:
    """Normalize a venue open order into :class:`OrderRecord`.

    ``client_key`` is supplied by the caller because **the venue does not carry
    one** -- the CLOB accepts no client-supplied order id (see
    :meth:`deepflow.ports.execution.ExecutionPort.find_by_intent`). Left absent, the
    venue's own order id stands in, which keeps the record self-describing rather
    than silently attributing it to the wrong intent.

    ``average_fill_price`` is the order's limit price rather than a true average: an
    open order exposes ``price``, ``original_size`` and ``size_matched`` and no
    per-fill detail. Recorded because it is the best available and named here so
    nobody mistakes it for a realized average -- the realized figure comes from the
    trade history.
    """
    size = Decimal(str(getattr(sdk_order, "original_size", 0) or 0))
    filled = Decimal(str(getattr(sdk_order, "size_matched", 0) or 0))
    order_id = getattr(sdk_order, "id", None)

    return OrderRecord(
        client_key=client_key or ClientOrderKey(str(order_id or "")),
        order_id=OrderId(str(order_id)) if order_id else None,
        status=to_order_status(getattr(sdk_order, "status", None), filled=filled, size=size),
        filled_shares=filled,
        average_fill_price=(
            Decimal(str(sdk_order.price)) if getattr(sdk_order, "price", None) else None
        ),
        submitted_at=getattr(sdk_order, "created_at", None),
        updated_at=getattr(sdk_order, "created_at", None),
    )


#: Shares held, as the venue names the field.
#:
#: ``polymarket.models.data.portfolio.Position`` calls it **``current_size``**. It has
#: no ``size``, ``shares`` or ``quantity``, and reading one of those silently yields
#: zero -- which then reads as a flat account holding inventory (finding 68).
#: ``total_size`` is a different quantity and is deliberately not a fallback.
POSITION_SIZE_FIELD: Final = "current_size"

#: Token id, as the venue names it on a *position*.
#:
#: ``asset_id`` on the model, aliased from ``token_id`` on the wire -- not ``asset``,
#: which is what the Data API's raw portfolio payload uses. Both are checked because
#: this mapper is fed from both shapes.
POSITION_TOKEN_FIELDS: Final = ("asset_id", "asset")


def position_shares(sdk_position: Any) -> Decimal:
    """Shares held. Shared so the mapper and the zero-filter cannot disagree.

    They did disagree, and that is the whole reason this is a function: two call
    sites each guessed at a field name the model does not have, so every position
    mapped to zero shares *and* every position was then filtered out for being zero.
    A single reader makes the next rename one failure instead of two silent ones.
    """
    return Decimal(str(getattr(sdk_position, POSITION_SIZE_FIELD, 0) or 0))


def to_position(sdk_position: Any) -> Position:
    """Normalize a venue position.

    ``entry_probability`` is set to the average entry price, which is the same number
    read as a probability -- a contract bought at 0.95 embeds a 95% implied view. It
    is *not* the model probability that justified the trade; that lives in the
    journal, and the venue has no idea it existed.

    ``opened_at`` cannot be filled from the venue: the position model carries
    ``end_date`` and ``last_event_at`` but **no open time**, and last activity is not
    an open time. ``_EPOCH`` is an explicit "the venue did not say" sentinel rather
    than a plausible default, and the local journal is the authority for this field.
    Reconciliation therefore must not diff ``opened_at`` between the two sides.
    """
    shares = position_shares(sdk_position)
    entry = Decimal(str(getattr(sdk_position, "avg_price", 0) or 0))
    condition_id = getattr(sdk_position, "condition_id", "") or ""
    token_id = ""
    for field in POSITION_TOKEN_FIELDS:
        token_id = getattr(sdk_position, field, "") or ""
        if token_id:
            break

    return Position(
        position_id=PositionId(f"{condition_id}:{token_id}"),
        condition_id=ConditionId(str(condition_id)),
        token_id=ClobTokenId(str(token_id)),
        shares=shares,
        average_entry_price=entry,
        entry_probability=entry,
        opened_at=_EPOCH,
        realized_pnl=Decimal(str(getattr(sdk_position, "realized_pnl", 0) or 0)),
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
