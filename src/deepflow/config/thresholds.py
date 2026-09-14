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
    """Collateral is pUSD, not USDC; the name is kept for continuity."""
    require_moneyline: bool = True
    """Sports markets also list spreads and totals. A win-probability model
    applied to a spread or an over/under prices a different question and returns
    a plausible-looking answer, so the market type is checked rather than
    assumed. Filterable at discovery via ``sports_market_types``."""
    max_spread_bps: Decimal = Field(default=Decimal(150), ge=0)
    max_slippage_bps: Decimal = Field(default=Decimal(100), ge=0)
    max_data_age_seconds: float = Field(default=5.0, gt=0)
    min_confidence: int = Field(default=70, ge=0, le=100)


#: Execution-quality limits for the event-driven categories: politics, geopolitics,
#: war/conflict, ceasefire and military-diplomatic.
#:
#: Measured, and they are nothing like the crypto ones. Across 30 live books on 16 politics
#: markets: median spread **39 bps** (min 10, max 5,000), median ask-side depth **2.7 M pUSD**
#: (min 130 K), tick size **0.001** — ten times finer than the up/down markets' 0.01. So a
#: cap that is impossible on a 5-minute crypto book is generous here, which is exactly why
#: these numbers are not shared.
#:
#: Freshness is looser on purpose. These markets have no continuously observable state: the
#: true probability sits still between announcements rather than drifting, so a book a few
#: seconds old is not stale in the way a 5-minute crypto book is. Not *arbitrarily* loose,
#: because the repricing when an announcement lands is fast and that is when a stale book is
#: most dangerous.
#:
#: The geopolitics figure is inherited rather than measured: only 2 of the 100 markets
#: sampled classified as GEOPOLITICS, which is too few to characterise. Recorded here so the
#: number is not mistaken for an observation.
EVENT_MARKET_LIMITS_NOTE = "measured on politics markets; geopolitics sample was 2 markets"


class SportsThresholds(BaseModel):
    """Section 7 candidate zones. Ranges, not buy conditions.

    Data-availability caveat, which the bands cannot express. Two sources carry
    live state and they do not cover the same sports: the socket
    (:mod:`deepflow.adapters.polymarket.sports_feed`) covers NFL, NHL, MLB, NBA,
    CBB, CFB, Soccer, Esports and Tennis, and Gamma's event index
    (:mod:`deepflow.adapters.polymarket.games`) covers whatever is in play,
    cricket included. Both carry only score / period / elapsed / status (plus
    possession for NFL and CFB). So:

    * ``football`` and ``tennis`` have a venue-native state feed, but a
      score-and-clock one -- no xG, shots, cards, or server. The richer
      ``FootballState`` / ``TennisState`` fields need a third-party provider.
    * ``cricket`` has live state through the event index -- a fixture was observed
      at ``score="74-100"``, ``period="Live"``. What it lacks is a rules module to
      read that state, which is unwritten work rather than a procurement gap. This
      docstring claimed the opposite until 2026-09-13, and ``CricketEngine`` was
      disabled on it.
    * ``badminton`` has no observed live state on either source. Recorded as not
      yet seen rather than as proven absent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    football: StrategyThresholds = StrategyThresholds(
        candidate_band=ProbabilityBand(low=Decimal("0.85"), high=Decimal("0.98"))
    )
    cricket: StrategyThresholds = StrategyThresholds(
        candidate_band=ProbabilityBand(low=Decimal("0.80"), high=Decimal("0.98"))
    )
    """Live state exists via the event index; the rules module to read it does not."""
    tennis: StrategyThresholds = StrategyThresholds(
        candidate_band=ProbabilityBand(low=Decimal("0.85"), high=Decimal("0.98"))
    )
    badminton: StrategyThresholds = StrategyThresholds(
        candidate_band=ProbabilityBand(low=Decimal("0.85"), high=Decimal("0.98"))
    )
    """No live state observed on either venue source. Not proven absent."""


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

    # --- Execution-quality limits, measured rather than borrowed ------------
    # These exist separately from the sports defaults because a borrowed number was
    # unsatisfiable here: `StrategyThresholds.max_spread_bps` is 150, and **one 0.01 tick at
    # a price near 0.5 is already 200 bps**, so no up/down market near the money could ever
    # pass it. Measured over 59,048 recorded snapshots of these markets: 97% two-sided,
    # median spread 217 bps, p90 833. A live sample of 28 books gave a median of 267.
    max_spread_bps: Decimal = Field(default=Decimal(300), ge=0)
    """Admits the measured median with headroom. Below 200 is unsatisfiable by tick
    arithmetic alone, which is worth knowing before tightening it."""

    max_slippage_bps: Decimal = Field(default=Decimal(200), ge=0)
    """One 0.01 tick near the money. Walking two ticks into this book is not a fill at a
    price the model priced."""

    max_data_age_seconds: float = Field(default=3.0, gt=0)
    """Tighter than any other strategy here. The TWAP publishes about every two seconds and
    the whole edge decays in minutes, so a five-second-old book is a different market."""

    min_liquidity_usdc: Decimal = Field(default=Decimal(1000), ge=0)
    """From measured ask-side notional: median 5,213 pUSD across 28 live books, min 125.
    Set well under the median so a normal book passes, and above the thin tail so a
    near-empty one does not. Collateral is pUSD; the name is kept for continuity with
    ``StrategyThresholds``."""


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
    """Client-side timeout, enforced by cancelling. It cannot be delegated to a
    GTD expiry: the venue requires an expiration at least 3 minutes out and
    expires the order a minute early, so ~2 minutes is the shortest GTD life
    available. Anything shorter is GTC plus our own cancel."""
    max_reprice_attempts: int = Field(default=3, ge=0)
    reprice_interval_seconds: float = Field(default=2.0, gt=0)
    max_submit_retries: int = Field(default=2, ge=0)
    """Only ever applied to errors proven not to have executed."""
    allow_market_orders: bool = False
    max_consecutive_execution_errors: int = Field(default=5, ge=1)

    max_seconds_delay: int = Field(default=0, ge=0)
    """Refuse markets whose venue-imposed matching delay
    (``market.trading.seconds_delay``) exceeds this.

    Zero by default, which excludes delayed-matching markets entirely. On such a
    market an order is accepted as ``delayed`` with no fill and no trade id, so
    every entry outlives ``order_timeout_seconds`` and every fill arrives after
    the edge it was priced on has gone. Raising this is a deliberate choice to
    trade blind through the delay window."""

    require_order_heartbeat: bool = True
    """Arm the venue-side dead-man's switch, which cancels our resting orders if
    no heartbeat arrives within 10 seconds.

    On by default because it is the only protection that survives this process
    dying: every circuit breaker here assumes a live supervisor, and a crashed
    bot otherwise leaves orders resting with nothing watching them."""

    engine_restart_backoff_base_seconds: float = Field(default=2.0, gt=0)
    engine_restart_max_wait_seconds: float = Field(default=180.0, gt=0)
    """HTTP 425 is an announced maintenance restart, not a fault. Wait it out
    rather than tripping the API_FAILURE breaker; expect a further 2-minute
    post-only window once orders are accepted again."""

    fail_closed_on_unknown_fee_schedule: bool = True
    """Refuse to trade a market flagged ``fees_enabled`` whose fee schedule did
    not come through. The unknown is always in one direction -- an unpriced
    taker fee makes net EV look better than it is -- and in the 0.85-0.98 band
    the fee is a material fraction of the whole edge."""


class MicrostructureThresholds(BaseModel):
    """Section 5. How order-flow features are measured."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    depth_band: Decimal = Field(default=Decimal("0.01"), gt=0, lt=1)
    """Price distance from the touch within which depth counts toward imbalance.

    Not a tuning detail -- it is the definition. Measured on live books, imbalance
    computed over the whole book is dominated by dust resting at 0.001 and 0.999,
    and the sign flips with the band: one market read -0.88 within a cent, +0.17
    within five, and -0.87 across the whole book. A number called "book imbalance"
    without a stated band is an artifact of where people park orders, not a signal.

    One cent is the default because these are 0-1 contracts, so a cent is
    comparable at any price level, and it is roughly the depth a taker of ordinary
    size actually sweeps."""

    confirm_band_multiple: Decimal = Field(default=Decimal(5), gt=1)
    """Second, wider band used only to check the first one's robustness."""

    min_depth_for_imbalance: Decimal = Field(default=Decimal(100), gt=0)
    """Below this combined depth inside the band, imbalance is not computed at all.
    A ratio of two tiny numbers is noise wearing a signal's clothing."""

    strong_imbalance: Decimal = Field(default=Decimal("0.40"), gt=0, le=1)
    weak_imbalance: Decimal = Field(default=Decimal("0.15"), gt=0, le=1)
    """Thresholds separating STRONG_BUY / BUY / NEUTRAL and their mirrors."""

    concentration_warning: Decimal = Field(default=Decimal("0.60"), gt=0, le=1)
    """Fraction of banded depth at a single level above which the book is treated as
    one cancellation from empty rather than liquid."""

    abnormal_move_sigma: Decimal = Field(default=Decimal(3), gt=0)
    """Move size, in multiples of the market's own recent volatility, that counts as
    abnormal. Relative rather than absolute: a 2c move is nothing on a 0.50 market
    and enormous on a 0.02 one."""

    min_history_for_velocity: int = Field(default=3, ge=2)
    """Snapshots needed before velocity is reported. Two points give a slope with no
    way to tell a trend from a single tick."""


class CircuitBreakerThresholds(BaseModel):
    """Section 21."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_data_age_seconds: float = Field(default=10.0, gt=0)
    degraded_age_fraction: Decimal = Field(default=Decimal("0.5"), gt=0, lt=1)
    """Fraction of the age budget past which a snapshot is DEGRADED rather than
    FRESH.

    The grey zone exists because freshness is not a step function. A snapshot at
    90% of its budget is about to expire, and opening a position on it means the
    data is stale before the order is even acknowledged. Degrading early stops new
    entries while still permitting exits, which is the asymmetry that matters:
    refusing to act on an open position is the worse failure."""

    max_settlement_failures_per_hour: int = Field(default=1, ge=0)
    """A matched trade that fails to settle on chain means local state and the
    venue's disagree about a position we thought was confirmed. One is enough to
    stop and look."""
    max_websocket_reconnects_per_hour: int = Field(default=20, ge=1)
    max_api_error_rate: Decimal = Field(default=Decimal("0.20"), ge=0, le=1)
    abnormal_slippage_bps: Decimal = Field(default=Decimal(300), gt=0)
    auto_resume: bool = False
    """Breakers latch. A human decides when the cause is actually fixed."""


class ExitThresholds(BaseModel):
    """Section 9. The noise band, and how far a position must move to be believed.

    Every number here exists to answer one question: has the position changed, or has
    the book merely wobbled? Exiting on a wobble pays the spread twice and turns a
    positive-EV trade into a realized loss, repeatedly -- and at the prices this system
    trades (0.85-0.98) the spread is a large fraction of the whole edge.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    noise_band: Decimal = Field(default=Decimal("0.03"), gt=0, le=1)
    """Adverse probability move treated as noise by default.

    3 points at 0.94 is roughly half the distance to 0.90, and on a thin outcome book a
    single impatient seller moves it that far. Scaled per market by realized volatility
    rather than used raw -- see ``volatility_multiple``."""

    volatility_multiple: Decimal = Field(default=Decimal(2), gt=0)
    """Band as a multiple of the market's own realized volatility, when it is measured.

    A fixed band is too tight on a 5-minute BTC market and far too loose on a settled
    football market, so the measured band wins when available. ``None`` volatility falls
    back to ``noise_band`` rather than to zero: an unmeasured volatility must not make
    every tick significant."""

    min_noise_band: Decimal = Field(default=Decimal("0.01"), gt=0, le=1)
    max_noise_band: Decimal = Field(default=Decimal("0.15"), gt=0, le=1)
    """Bounds on the scaled band. A volatility estimate near zero would otherwise make
    the band vanish and every tick a trigger; a spike would make it so wide that a real
    state change reads as noise."""

    full_exit_score: int = Field(default=70, ge=0, le=100)
    partial_exit_score: int = Field(default=40, ge=0, le=100)
    """Score at which deterioration stops being tolerable. Two thresholds rather than
    one because the middle band has a genuinely different answer: reduce size, keep the
    thesis."""

    partial_exit_fraction: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)
    """How much of the position a PARTIAL_EXIT closes."""

    add_min_probability_gain: Decimal = Field(default=Decimal("0.02"), gt=0, le=1)
    """Model improvement required before adding. Adding is a new trade and goes through
    the same risk limits as an entry, so the bar is a real edge, not a flat tick."""

    late_window_seconds: int = Field(default=300, ge=0)
    late_window_band_multiple: Decimal = Field(default=Decimal("0.5"), gt=0, le=1)
    """Inside this many seconds of resolution the band tightens by this multiple.

    Close to resolution a small adverse move is far more informative than the same move
    an hour earlier: there is less time left for it to mean-revert, and less time for the
    thesis to be right. The same 2-point drop is noise at 60 minutes and a warning at 60
    seconds."""

    emergency_exit_uses_market_order: bool = True
    """A verified state change exits at market.

    The thesis is void, so the risk of not getting out dominates the cost of crossing
    the spread -- which is the opposite of the trade-off everywhere else in this
    system."""


class Thresholds(BaseModel):
    """Root of the tunable tree."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    classification_min_confidence: Decimal = Field(default=Decimal("0.75"), ge=0, le=1)
    sports: SportsThresholds = SportsThresholds()
    microstructure: MicrostructureThresholds = MicrostructureThresholds()
    late_game: LateGameThresholds = LateGameThresholds()
    btc_5m: Btc5mThresholds = Btc5mThresholds()
    smart_money: SmartMoneyThresholds = SmartMoneyThresholds()
    geopolitics: GeopoliticsThresholds = GeopoliticsThresholds()
    cross_market: CrossMarketThresholds = CrossMarketThresholds()
    risk: RiskLimits = RiskLimits()
    execution: ExecutionThresholds = ExecutionThresholds()
    exits: ExitThresholds = ExitThresholds()

    event_markets: StrategyThresholds = StrategyThresholds(
        candidate_band=ProbabilityBand(low=Decimal("0.85"), high=Decimal("0.98")),
        # See EVENT_MARKET_LIMITS_NOTE: median spread 39 bps and median depth 2.7M pUSD across
        # 30 live books, so these are generous against what was measured rather than against
        # what a sports market looks like.
        max_spread_bps=Decimal(100),
        max_slippage_bps=Decimal(100),
        max_data_age_seconds=10.0,
        min_liquidity_usdc=Decimal(50_000),
        require_moneyline=False,
    )
    """Politics, geopolitics, war/conflict, ceasefire and military-diplomatic markets."""
    breakers: CircuitBreakerThresholds = CircuitBreakerThresholds()
