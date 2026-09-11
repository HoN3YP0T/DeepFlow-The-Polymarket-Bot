"""Dashboard response schemas. Section 22.

Wire contracts for the React/Next.js frontend, kept separate from the domain
models so an internal refactor does not silently break the dashboard.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

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


class RiskActionRequest(Schema):
    """Destructive controls require an explicit typed confirmation, so a
    mis-click cannot close the book."""

    confirm: str
    reason: str = ""
