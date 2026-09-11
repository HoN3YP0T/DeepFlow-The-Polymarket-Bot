// Wire types mirroring deepflow/api/schemas.py.
// Keep in sync with the Python schemas; these are the dashboard's contract.

export type RunMode = "BACKTEST" | "PAPER" | "SHADOW" | "LIVE";

export type ComponentHealth = "UP" | "DEGRADED" | "DOWN" | "UNKNOWN";

export interface Overview {
  mode: RunMode;
  balance_usdc: string;
  available_capital_usdc: string;
  reserved_capital_usdc: string;
  total_exposure_usdc: string;
  pnl_today_usdc: string;
  pnl_total_usdc: string;
  drawdown_fraction: string;
  open_positions: number;
  pending_orders: number;
  entries_halted: boolean;
  halt_reasons: string[];
}

export interface SmartMoneyEntryView {
  wallet: string;
  side: string;
  notional_usdc: string;
  /** Exact odds paid. Displayed verbatim -- rounding loses the signal. */
  entry_price: string;
  current_price: string | null;
  is_exit: boolean;
  observed_at: string;
}

export interface AvailableTradeCard {
  condition_id: string;
  token_id: string;
  title: string;
  category: string;
  market_probability: string;
  model_probability: string;
  edge: string;
  net_ev: string;
  confidence: number;
  smart_money_score: number | null;
  smart_money_entries: SmartMoneyEntryView[];
  smart_money_combined_usdc: string | null;
  flow: string;
  liquidity: string;
  risk: string;
  game_state: string | null;
  time_remaining_seconds: number | null;
  action: string;
}

export interface OpenPositionCard {
  position_id: string;
  title: string;
  category: string;
  entry_price: string;
  current_price: string;
  shares: string;
  unrealized_pnl_usdc: string;
  entry_probability: string;
  model_probability: string | null;
  current_probability: string;
  smart_money_state: string | null;
  exit_score: number;
  event_state: string | null;
  time_remaining_seconds: number | null;
}

export interface WhaleActivityCard {
  wallet: string;
  smart_score: number;
  condition_id: string;
  title: string;
  side: string;
  notional_usdc: string;
  entry_price: string;
  current_price: string | null;
  is_new_wallet: boolean;
  is_exit: boolean;
  observed_at: string;
}

export interface ComponentStatus {
  name: string;
  status: ComponentHealth;
  latency_ms: number | null;
  data_age_seconds: number | null;
  reconnect_count: number;
  error_count: number;
  detail: string | null;
}
