"""Tunable strategy and risk thresholds.

Every number the specification quotes as an example lives here, not inline in
an engine. They are defaults, not constants: the dashboard can override them
at runtime and a backtest can sweep them.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ProbabilityBand(BaseModel):
    """A candidate zone -- a range that makes a market *worth evaluating*.

    Being inside the band is never sufficient to trade. It only decides which
    markets the engines spend cycles on.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    low: Decimal = Field(ge=0, le=1)
    high: Decimal = Field(ge=0, le=1)

    @model_validator(mode="after")
    def _ordered(self) -> ProbabilityBand:
        if self.low >= self.high:
            raise ValueError("band low must be < high")
        return self

    def contains(self, probability: Decimal) -> bool:
        return self.low <= probability <= self.high


class StrategyThresholds(BaseModel):
    """Per-strategy gates applied on top of the global safety checklist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    """Every strategy is off until switched on deliberately."""

    candidate_band: ProbabilityBand
    min_model_confirmation: Decimal = Field(default=Decimal("0.02"), ge=0)
    """Model probability must exceed market probability by at least this much."""
    min_net_ev: Decimal = Field(default=Decimal("0.005"), ge=0)
    min_liquidity_usdc: Decimal = Field(default=Decimal(5000), ge=0)
    max_spread_bps: Decimal = Field(default=Decimal(150), ge=0)
    max_slippage_bps: Decimal = Field(default=Decimal(100), ge=0)
    max_data_age_seconds: float = Field(default=5.0, gt=0)
    min_confidence: int = Field(default=70, ge=0, le=100)


class SportsThresholds(BaseModel):
    """Section 7 candidate zones. Ranges, not buy conditions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    football: StrategyThresholds = StrategyThresholds(
        candidate_band=ProbabilityBand(low=Decimal("0.85"), high=Decimal("0.98"))
    )
    cricket: StrategyThresholds = StrategyThresholds(
        candidate_band=ProbabilityBand(low=Decimal("0.80"), high=Decimal("0.98"))
    )
    tennis: StrategyThresholds = StrategyThresholds(
        candidate_band=ProbabilityBand(low=Decimal("0.85"), high=Decimal("0.98"))
    )
    badminton: StrategyThresholds = StrategyThresholds(
        candidate_band=ProbabilityBand(low=Decimal("0.85"), high=Decimal("0.98"))
    )


class LateGameThresholds(BaseModel):
    """Section 8. Time-based entry windows for clock sports."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    football_min_minute: int = Field(default=80, ge=0, le=120)
    football_min_goal_difference: int = Field(default=2, ge=1)
    min_seconds_remaining: int = Field(default=60, ge=0)
    """Below this, execution risk and resolution lag dominate any edge."""


class Btc5mThresholds(BaseModel):
    """Section 10. BTC 5-minute markets get their own gates."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    candidate_band: ProbabilityBand = ProbabilityBand(low=Decimal("0.90"), high=Decimal("0.98"))
    min_seconds_to_expiry: int = Field(default=20, ge=0)
    max_seconds_to_expiry: int = Field(default=300, ge=0)
    min_edge_over_costs: Decimal = Field(default=Decimal("0.015"), ge=0)
    """Model probability must clear market + fees + spread + slippage by this
    margin before a late-expiry entry is considered."""
    uncertainty_buffer: Decimal = Field(default=Decimal("0.01"), ge=0)


class SmartMoneyThresholds(BaseModel):
    """Section 11."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    min_notional_usdc: Decimal = Field(default=Decimal(10_000), ge=0)
    min_smart_score: int = Field(default=70, ge=0, le=100)
    min_wallets_for_cluster: int = Field(default=2, ge=1)
    lookback_days: int = Field(default=90, ge=1)
    max_weight_in_signal: Decimal = Field(default=Decimal("0.25"), ge=0, le=1)
    """Hard cap on how much smart-money evidence can move a probability.
    Copying whales is not a strategy; it is one weighted feature."""


class GeopoliticsThresholds(BaseModel):
    """Sections 13-14."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    min_source_reliability: Decimal = Field(default=Decimal("0.80"), ge=0, le=1)
    min_corroborating_sources: int = Field(default=2, ge=1)
    unverified_social_weight: Decimal = Field(default=Decimal(0), ge=0, le=1)
    """Unverified social media is not confirmation. Zero by default."""


class CrossMarketThresholds(BaseModel):
    """Section 15. Off until backtested -- logical relationships are the
    easiest place to lose money to a resolution technicality."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    backtest_validated: bool = False
    min_inconsistency: Decimal = Field(default=Decimal("0.03"), ge=0)


class RiskLimits(BaseModel):
    """Section 17. All fractions are of current bankroll unless noted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_position_usdc: Decimal = Field(default=Decimal(500), gt=0)
    max_position_fraction: Decimal = Field(default=Decimal("0.02"), gt=0, le=1)
    max_event_exposure_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    max_correlated_exposure_fraction: Decimal = Field(default=Decimal("0.10"), gt=0, le=1)
    max_strategy_exposure_fraction: Decimal = Field(default=Decimal("0.20"), gt=0, le=1)
    max_total_exposure_fraction: Decimal = Field(default=Decimal("0.50"), gt=0, le=1)
    max_daily_loss_fraction: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    max_drawdown_fraction: Decimal = Field(default=Decimal("0.15"), gt=0, le=1)
    max_open_positions: int = Field(default=10, gt=0)
    reserved_capital_fraction: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    """Never deployed. Covers settlement lag and emergency exits."""

    kelly_fraction: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)
    """Capped fractional Kelly. Full Kelly is never used -- it is optimal only
    under a correct probability, and ours is an estimate."""
    kelly_hard_cap: Decimal = Field(default=Decimal("0.05"), gt=0, le=1)
    """Absolute ceiling on any single sizing output, whatever Kelly says."""


class ExecutionThresholds(BaseModel):
    """Section 19."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    default_order_type_is_marketable_limit: bool = True
    order_timeout_seconds: float = Field(default=10.0, gt=0)
    max_reprice_attempts: int = Field(default=3, ge=0)
    reprice_interval_seconds: float = Field(default=2.0, gt=0)
    max_submit_retries: int = Field(default=2, ge=0)
    """Only ever applied to errors proven not to have executed."""
    allow_market_orders: bool = False
    max_consecutive_execution_errors: int = Field(default=5, ge=1)


class CircuitBreakerThresholds(BaseModel):
    """Section 21."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_data_age_seconds: float = Field(default=10.0, gt=0)
    max_websocket_reconnects_per_hour: int = Field(default=20, ge=1)
    max_api_error_rate: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    abnormal_slippage_bps: Decimal = Field(default=Decimal(300), gt=0)
    auto_resume: bool = False
    """Breakers latch. A human decides when the cause is actually fixed."""


class Thresholds(BaseModel):
    """Root of the tunable tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classification_min_confidence: Decimal = Field(default=Decimal("0.75"), ge=0, le=1)
    sports: SportsThresholds = SportsThresholds()
    late_game: LateGameThresholds = LateGameThresholds()
    btc_5m: Btc5mThresholds = Btc5mThresholds()
    smart_money: SmartMoneyThresholds = SmartMoneyThresholds()
    geopolitics: GeopoliticsThresholds = GeopoliticsThresholds()
    cross_market: CrossMarketThresholds = CrossMarketThresholds()
    risk: RiskLimits = RiskLimits()
    execution: ExecutionThresholds = ExecutionThresholds()
    breakers: CircuitBreakerThresholds = CircuitBreakerThresholds()
