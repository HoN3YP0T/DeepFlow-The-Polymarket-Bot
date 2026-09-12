"""Domain models.

These are the normalized, venue-agnostic shapes the pipeline speaks. Nothing
here imports the Polymarket SDK: adapters translate into these types at the
boundary (``adapters/polymarket/mapping.py``), so replacing or upgrading the
SDK never ripples past the adapter layer.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deepflow.core.enums import (
    DataQuality,
    ExitAction,
    MarketCategory,
    OrderSide,
    OrderStatus,
    OrderType,
    OutcomeSide,
    ResolutionValidity,
    SignalAction,
    TimeInForce,
)
from deepflow.core.types import (
    ClientOrderKey,
    ClobTokenId,
    ConditionId,
    EventId,
    OrderId,
    PositionId,
    SignalId,
    WalletAddress,
)


class Frozen(BaseModel):
    """Immutable base. Domain values are snapshots, not mutable scratch space."""

    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)


# ===========================================================================
# Market identity and discovery
# ===========================================================================
class Outcome(Frozen):
    """One tradeable leg of a market."""

    token_id: ClobTokenId
    label: str
    side: OutcomeSide | None = None
    """Set when the outcome maps cleanly onto binary YES/NO."""


class FeeSchedule(Frozen):
    """A market's fee parameters, as the venue reports them.

    Read from the market rather than inferred from its category. The published
    per-category table (see ``adapters.polymarket.venue``) is a planning
    fallback; a recategorised market will disagree with it, and the market is
    authoritative.
    """

    rate: Decimal = Field(ge=0)
    """Base taker rate, e.g. 0.04."""
    exponent: Decimal = Decimal(1)
    """Exponent on the ``p * (1 - p)`` price component."""
    taker_only: bool = True
    """Makers are never charged on Polymarket."""
    rebate_rate: Decimal = Decimal(0)
    """Maker rebate as a fraction of the taker fee. Income for resting orders,
    not a cost -- excluded from :class:`CostBreakdown` and relevant only if this
    system ever quotes rather than takes."""


class Market(Frozen):
    """A normalized market as discovered, before any pricing is attached."""

    condition_id: ConditionId
    event_id: EventId | None = None
    question: str
    slug: str | None = None
    outcomes: tuple[Outcome, ...]
    active: bool
    closed: bool
    accepting_orders: bool
    start_date: datetime | None = None
    """When the market opened.

    Carried because ``end_date - start_date`` is the market's *window*, which is
    what separates a short-dated strike market from a long-horizon forecast. Time
    remaining cannot answer that: any market is short-dated an hour before it
    settles."""
    end_date: datetime | None = None
    tags: tuple[str, ...] = ()
    """Venue tag slugs, e.g. ``("sports", "cricket")``.

    Populated only when discovery asks for them -- the venue omits tags unless
    ``include_tag=True`` is passed, and an empty tuple therefore means "not
    requested" as often as it means "untagged". Kept as slugs because they are what
    a human reads in a journal entry."""

    tag_ids: tuple[str, ...] = ()
    """Venue tag ids, e.g. ``("1", "517")``.

    The classifier keys on these rather than on slugs: ``get_sports()`` publishes
    its league-to-tag mapping as ids, so ids are the join key to venue-maintained
    metadata, and a slug rename would silently break a match where an id would
    not."""
    resolution_source: str | None = None
    resolution_text: str | None = None
    minimum_tick_size: Decimal | None = None
    """Minimum price increment. Changes at runtime -- the market stream emits
    ``tick_size_change`` -- and an order priced off a stale value is rejected,
    so a stored tick size must be refreshed from that event rather than cached
    for the life of the market."""
    minimum_order_size: Decimal | None = None
    """Venue-documented as a minimum *collateral notional* per order, not a
    share count. Sizing must therefore check ``shares * price``; comparing a
    share quantity against it passes the check at 0.95 and fails it at 0.05."""
    negative_risk: bool = False
    """Member of a negative-risk group. Selects the exchange contract used as
    the EIP-712 verifying contract, so it is a signing input, not a label."""
    enable_order_book: bool = True
    """A market can exist and be discoverable before its book opens."""

    fee_type: str | None = None
    """The venue's own fee category, e.g. ``politics_fees``, ``sports_fees_v2``.

    A coarse but authoritative category hint. Coarse because it distinguishes
    politics from sports and nothing finer; authoritative because the venue charges
    on it. Used to corroborate a classification, never to create one -- and it is
    absent on a noticeable share of markets."""

    fees_enabled: bool = False
    fee_schedule: FeeSchedule | None = None
    """``None`` means unknown, not free. Only geopolitics markets are documented
    as genuinely fee-free; everything else charges the taker."""

    seconds_delay: int = 0
    """Venue-imposed matching delay. A submitted order comes back ``delayed``
    rather than ``matched``, with no fills and no trade ids. On such a market an
    entry cannot be confirmed inside a short order timeout, which makes it
    incompatible with a strategy whose edge decays in seconds."""

    game_start_time: datetime | None = None
    """Scheduled start, for sports markets."""

    sports_market_type: str | None = None
    """``moneyline`` / ``spreads`` / ``totals``. A win-probability model applies
    only to a moneyline; pointing it at a spread or a total prices the wrong
    question with an answer that looks plausible."""

    def outcome_for(self, token_id: ClobTokenId) -> Outcome | None:
        return next((o for o in self.outcomes if o.token_id == token_id), None)


class Classification(Frozen):
    """Classifier verdict.

    ``category`` is UNKNOWN whenever ``confidence`` falls below the configured
    threshold. Section 3: uncertain classification must never auto-trade.
    """

    category: MarketCategory
    confidence: Decimal = Field(ge=0, le=1)
    rationale: str
    matched_signals: tuple[str, ...] = ()

    @property
    def is_tradeable(self) -> bool:
        return self.category is not MarketCategory.UNKNOWN


class ResolutionCriteria(Frozen):
    """Parsed resolution rules. Section 4.

    A market is tradeable only when ``validity is VALID``. The bot must never
    infer the payout condition from the market title alone -- ``yes_condition``
    and ``no_condition`` have to come from the resolution text.
    """

    validity: ResolutionValidity
    yes_condition: str | None = None
    no_condition: str | None = None
    deadline: datetime | None = None
    timezone_name: str | None = None
    qualifying_events: tuple[str, ...] = ()
    non_qualifying_events: tuple[str, ...] = ()
    primary_source: str | None = None
    source_hierarchy: tuple[str, ...] = ()
    ambiguity_rules: str | None = None
    cancellation_rules: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def is_valid(self) -> bool:
        return self.validity is ResolutionValidity.VALID


# ===========================================================================
# Live market state
# ===========================================================================
class BookLevel(Frozen):
    price: Decimal
    size: Decimal


class OrderBook(Frozen):
    """One side-pair snapshot for a single outcome token."""

    token_id: ClobTokenId
    bids: tuple[BookLevel, ...]
    """Descending by price."""
    asks: tuple[BookLevel, ...]
    """Ascending by price."""
    captured_at: datetime

    @model_validator(mode="after")
    def _check_ordering(self) -> OrderBook:
        """Reject a book whose levels are not sorted best-price-first.

        The crossed-book check alone is not enough. The venue sends each side
        worst-price-first, and a pass-through of that wire order gives
        ``bids[0]=0.001`` against ``asks[0]=0.999``: not crossed, so the old
        check accepted it, and every spread and slippage figure derived from it
        was wrong by the width of the book. Validating the ordering itself turns
        that silent absurdity into a loud failure at the boundary.
        """
        bid_prices = [level.price for level in self.bids]
        if bid_prices != sorted(bid_prices, reverse=True):
            raise ValueError("bids must be sorted descending (best bid first)")

        ask_prices = [level.price for level in self.asks]
        if ask_prices != sorted(ask_prices):
            raise ValueError("asks must be sorted ascending (best ask first)")

        if self.bids and self.asks and self.bids[0].price >= self.asks[0].price:
            raise ValueError("crossed book: best bid >= best ask")
        return self

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0].price if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0].price if self.asks else None

    @property
    def mid(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return (self.best_bid + self.best_ask) / 2

    @property
    def spread(self) -> Decimal | None:
        if self.best_bid is None or self.best_ask is None:
            return None
        return self.best_ask - self.best_bid


class PublicTrade(Frozen):
    """A trade printed on the tape."""

    token_id: ClobTokenId
    price: Decimal
    size: Decimal
    side: OrderSide
    """Aggressor side, where the venue reports it."""
    traded_at: datetime


class Microstructure(Frozen):
    """Derived order-flow features. Section 5.

    Every field is optional: a thin book legitimately cannot support an
    imbalance estimate, and a fabricated zero would read as a real signal.
    """

    book_imbalance: Decimal | None = None
    flow_imbalance: Decimal | None = None
    liquidity_concentration: Decimal | None = None
    estimated_slippage_bps: Decimal | None = None
    estimated_market_impact_bps: Decimal | None = None
    price_velocity: Decimal | None = None
    price_acceleration: Decimal | None = None
    abnormal_move: bool = False


class MarketSnapshot(Frozen):
    """Everything the engines are allowed to look at for one market, at one
    instant. Carrying freshness alongside the data makes "is this stale?" a
    property of the snapshot rather than a question asked of a global."""

    condition_id: ConditionId
    books: tuple[OrderBook, ...]
    recent_trades: tuple[PublicTrade, ...] = ()
    microstructure: Microstructure = Microstructure()
    volume_24h: Decimal | None = None
    liquidity: Decimal | None = None
    time_remaining_seconds: int | None = None
    captured_at: datetime
    quality: DataQuality = DataQuality.FRESH

    def book_for(self, token_id: ClobTokenId) -> OrderBook | None:
        return next((b for b in self.books if b.token_id == token_id), None)

    @property
    def entries_allowed(self) -> bool:
        """Only FRESH data may open new risk. Degraded data can still manage
        an existing position -- refusing to look at an open trade is worse."""
        return self.quality is DataQuality.FRESH


# ===========================================================================
# Probability, edge, expected value
# ===========================================================================
class ProbabilityEstimate(Frozen):
    """A single engine's view of an outcome. Section 16."""

    token_id: ClobTokenId
    model_probability: Decimal = Field(ge=0, le=1)
    calibrated_probability: Decimal = Field(ge=0, le=1)
    """Post-calibration. Raw model output is never used for sizing directly."""
    uncertainty: Decimal = Field(ge=0, le=1)
    """1-sigma band. Feeds the EV uncertainty buffer and Kelly haircut."""
    engine: str
    inputs: dict[str, str] = Field(default_factory=dict)
    """Human-readable feature snapshot for the journal."""

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())


class CostBreakdown(Frozen):
    """Every cost between a quoted edge and a realized one.

    All terms are basis points of notional so they sum. The fee term is the one
    the venue fixes for us: it is ``rate * (1 - p)`` in bps at the documented
    exponent of 1, which means it *grows* as a fraction of the entry as the
    price falls, even though the collateral fee is symmetric about 0.50.
    """

    fee_bps: Decimal = Decimal(0)
    spread_cost_bps: Decimal = Decimal(0)
    slippage_bps: Decimal = Decimal(0)
    uncertainty_buffer_bps: Decimal = Decimal(0)

    @property
    def total_bps(self) -> Decimal:
        return self.fee_bps + self.spread_cost_bps + self.slippage_bps + self.uncertainty_buffer_bps


class EvAssessment(Frozen):
    """Net expected value after everything. Section 16.

    ``edge`` is the headline number; ``net_ev`` is the one that decides.
    A trade requires ``net_ev > 0`` -- a positive edge that costs more than
    it is worth is not a trade.
    """

    token_id: ClobTokenId
    market_probability: Decimal = Field(ge=0, le=1)
    model_probability: Decimal = Field(ge=0, le=1)
    edge: Decimal
    costs: CostBreakdown
    fill_probability: Decimal = Field(ge=0, le=1)
    net_ev: Decimal
    confidence: int = Field(ge=0, le=100)

    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())

    @property
    def is_positive(self) -> bool:
        return self.net_ev > 0


# ===========================================================================
# Smart money
# ===========================================================================
class SmartMoneyEntry(Frozen):
    """One notable wallet action on a market. Section 11/12."""

    wallet: WalletAddress
    side: OrderSide
    outcome_label: str
    notional_usdc: Decimal
    entry_price: Decimal
    """Exact odds paid -- the dashboard shows this verbatim."""
    market_probability_at_entry: Decimal
    observed_at: datetime
    is_increase: bool = False
    is_exit: bool = False


class SmartMoneySignal(Frozen):
    """Aggregated wallet intelligence for one market.

    ``score`` is earned from historical performance, category specialization
    and timing -- explicitly not from position size alone. Size makes a whale;
    it does not make a whale correct.
    """

    condition_id: ConditionId
    score: int = Field(ge=0, le=100)
    entries: tuple[SmartMoneyEntry, ...] = ()
    exits: tuple[SmartMoneyEntry, ...] = ()
    combined_notional_usdc: Decimal = Decimal(0)
    contributing_wallets: int = 0
    rationale: str = ""


# ===========================================================================
# Signals, orders, positions
# ===========================================================================
class Signal(Frozen):
    """A fully-formed trade intent, before risk approval."""

    signal_id: SignalId
    condition_id: ConditionId
    token_id: ClobTokenId
    action: SignalAction
    category: MarketCategory
    probability: ProbabilityEstimate
    ev: EvAssessment
    smart_money: SmartMoneySignal | None = None
    target_price: Decimal
    generated_at: datetime
    rationale: str


class OrderIntent(Frozen):
    """What we want to do, with its idempotency key already fixed.

    The key is derived from the intent, not from the attempt, so a retry of
    the same intent cannot become a second order.
    """

    client_key: ClientOrderKey
    condition_id: ConditionId
    token_id: ClobTokenId
    side: OrderSide
    order_type: OrderType
    size_shares: Decimal
    limit_price: Decimal
    max_slippage_bps: Decimal
    expires_at: datetime | None = None
    """Only meaningful for a GTD order, and the venue's minimum applies: an
    expiry under ~2 minutes away is not expressible. Short working orders leave
    this ``None`` and are cancelled client-side."""

    time_in_force: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    """Reject rather than take if the order would cross. Required during the
    two-minute post-only window after a matching-engine restart, and the only
    way to guarantee maker treatment (and so a zero fee)."""

    max_spend: Decimal | None = None
    """All-in collateral cap for a BUY, fees included.

    A market BUY's amount is the *pre-fee* notional, with taker fees charged on
    top. Without this cap a position sized to the last cent of available
    collateral fails on balance, because the fee was never in the budget."""


class OrderRecord(Frozen):
    """Observed state of a submitted order."""

    client_key: ClientOrderKey
    order_id: OrderId | None
    status: OrderStatus
    filled_shares: Decimal = Decimal(0)
    average_fill_price: Decimal | None = None
    submitted_at: datetime | None = None
    updated_at: datetime | None = None
    error: str | None = None

    @property
    def needs_reconciliation(self) -> bool:
        return self.status is OrderStatus.UNKNOWN


class Position(Frozen):
    """An open or closed position."""

    position_id: PositionId
    condition_id: ConditionId
    token_id: ClobTokenId
    shares: Decimal
    average_entry_price: Decimal
    entry_probability: Decimal
    opened_at: datetime
    closed_at: datetime | None = None
    realized_pnl: Decimal = Decimal(0)

    @property
    def is_open(self) -> bool:
        return self.closed_at is None

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        return (mark_price - self.average_entry_price) * self.shares


class ExitDecision(Frozen):
    """Output of the exit engine. Section 9."""

    position_id: PositionId
    action: ExitAction
    exit_score: int = Field(ge=0, le=100)
    fraction: Decimal = Field(default=Decimal(0), ge=0, le=1)
    """Portion of the position to close. Ignored for HOLD/ADD."""
    reason: str
    triggers: tuple[str, ...] = ()
