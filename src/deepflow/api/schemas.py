"""Dashboard response schemas. Section 22.

Wire contracts for the React/Next.js frontend, kept separate from the domain
models so an internal refactor does not silently break the dashboard.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from deepflow.core.enums import ComponentHealth, MarketCategory, RunMode


class Schema(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", protected_namespaces=())


class OverviewResponse(Schema):
    """Top-of-dashboard summary."""

    mode: RunMode
    balance_usdc: Decimal
    available_capital_usdc: Decimal
    reserved_capital_usdc: Decimal
    total_exposure_usdc: Decimal
    pnl_today_usdc: Decimal
    pnl_total_usdc: Decimal
    drawdown_fraction: Decimal
    open_positions: int
    pending_orders: int
    entries_halted: bool
    halt_reasons: tuple[str, ...] = ()

    counters: dict[str, int] = Field(default_factory=dict)
    """The decision-chain counters, carried here so the dashboard shows them beside the
    capital figures rather than in a second request.

    Together, because each alone misleads: estimates without decisions says the model
    spoke and nothing acted, and decisions without the journal's write failures once meant
    3,642 rows that were never written (§83)."""


class SmartMoneyEntryView(Schema):
    wallet: str
    side: str
    notional_usdc: Decimal
    entry_price: Decimal
    """Exact odds paid, shown verbatim on the card."""
    current_price: Decimal | None = None
    is_exit: bool = False
    observed_at: datetime


class AvailableTradeCard(Schema):
    """One card in the Available Trades panel.

    Mirrors the section 12 layout: market and model probability, edge, net EV,
    smart-money detail with exact entry odds, flow, liquidity, risk, and the
    proposed action.
    """

    condition_id: str
    token_id: str
    title: str
    category: MarketCategory
    market_probability: Decimal
    model_probability: Decimal
    edge: Decimal
    net_ev: Decimal
    confidence: int = Field(ge=0, le=100)
    smart_money_score: int | None = None
    smart_money_entries: tuple[SmartMoneyEntryView, ...] = ()
    smart_money_combined_usdc: Decimal | None = None
    flow: str
    liquidity: str
    risk: str
    game_state: str | None = None
    time_remaining_seconds: int | None = None
    action: str


class OpenPositionCard(Schema):
    position_id: str
    title: str
    category: MarketCategory
    entry_price: Decimal
    current_price: Decimal
    shares: Decimal
    unrealized_pnl_usdc: Decimal
    entry_probability: Decimal
    model_probability: Decimal | None = None
    current_probability: Decimal
    smart_money_state: str | None = None
    exit_score: int = Field(ge=0, le=100)
    event_state: str | None = None
    time_remaining_seconds: int | None = None


class WhaleActivityCard(Schema):
    wallet: str
    smart_score: int
    condition_id: str
    title: str
    side: str
    notional_usdc: Decimal
    entry_price: Decimal
    current_price: Decimal | None = None
    is_new_wallet: bool = False
    is_exit: bool = False
    observed_at: datetime


class ComponentStatus(Schema):
    name: str
    status: ComponentHealth
    latency_ms: float | None = None
    data_age_seconds: float | None = None
    reconnect_count: int = 0
    error_count: int = 0
    detail: str | None = None


class HealthResponse(Schema):
    """System Health panel: SDK, CLOB, WebSocket, Data API, Relayer, DB,
    Redis, execution, probability engine, event feed, clock."""

    components: tuple[ComponentStatus, ...]
    checked_at: datetime


class StrategyToggle(Schema):
    name: str
    enabled: bool


class StrategyToggleRequest(Schema):
    """Turning an engine off is a deliberate act, so it carries a confirmation too.

    A lighter phrase than the risk controls': this stops one model forming opinions and
    is reversible in a click, where Close All Positions is neither.
    """

    enabled: bool
    confirm: str
    reason: str = ""


class RiskActionRequest(Schema):
    """Destructive controls require an explicit typed confirmation, so a
    mis-click cannot close the book."""

    confirm: str
    reason: str = ""


# ===========================================================================
# Authentication
# ===========================================================================
class LoginRequest(Schema):
    username: str = Field(min_length=1, max_length=120)
    password: str = Field(min_length=1, max_length=1024)
    """Capped at 1024 so an enormous body cannot turn a login into a scrypt-powered
    denial of service against ourselves."""


class LoginResponse(Schema):
    token: str
    role: str
    expires_in_seconds: int


class WhoAmIResponse(Schema):
    subject: str
    role: str
    may_operate: bool
    may_administer: bool
    """What the dashboard uses to decide which controls to *render*. Never the
    enforcement -- a hidden button is not a disabled one, so every control re-checks."""


# ===========================================================================
# Controls
# ===========================================================================
class ControlResponse(Schema):
    """What a control did, in terms an operator can verify afterwards."""

    action: str
    applied: bool
    """``False`` is a real, successful answer: the control ran, decided not to act, and
    said why. A control that cannot act must never report that it did."""

    detail: str
    actor: str
    at: datetime
    entries_halted: bool | None = None
    open_breakers: tuple[str, ...] = ()


class RiskLimitsView(Schema):
    """The live risk limits, as the next decision will read them."""

    bankroll_usdc: Decimal
    max_position_fraction: Decimal
    max_total_exposure_fraction: Decimal
    max_daily_loss_fraction: Decimal
    max_open_positions: int


class RiskLimitsUpdate(Schema):
    """A change to the live limits. Every field optional; omitted means unchanged.

    Deliberately not a whole-object replacement: a dashboard that PUTs the full set will
    happily reset a limit the operator never meant to touch, using whatever stale value its
    form was rendered with.
    """

    confirm: str
    reason: str = ""
    bankroll_usdc: Decimal | None = Field(default=None, ge=0)
    max_position_fraction: Decimal | None = Field(default=None, gt=0, le=1)
    max_total_exposure_fraction: Decimal | None = Field(default=None, gt=0, le=1)
    max_daily_loss_fraction: Decimal | None = Field(default=None, gt=0, le=1)
    max_open_positions: int | None = Field(default=None, gt=0)


class EngineView(Schema):
    name: str
    categories: tuple[str, ...]
    enabled: bool
    calibrated: bool
    """Whether a fitted curve is installed. ``False`` means the engine's raw output is
    used as-is, which is the honest default until a fit is activated."""


class PriorView(Schema):
    slug: str
    probability: Decimal
    uncertainty: Decimal
    source: str
    applied: bool
    """Whether a tracked market actually matched this slug. ``False`` means the prior is
    configured and reaching nothing -- usually a typo, which otherwise looks exactly like
    a market that has closed."""


class AuditEntryView(Schema):
    id: int
    actor: str
    action: str
    detail: dict[str, Any] | None = None
    occurred_at: datetime
