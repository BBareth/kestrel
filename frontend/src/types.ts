export type Mode = "paper" | "live";

export interface Ticker {
  symbol: string;
  price: number | null;
  mark_price: number | null;
  index_price: number | null;
  bid: number | null;
  ask: number | null;
  spread_bps: number | null;
  funding_rate: number | null;
  next_funding_ms: number | null;
  change_24h_pct: number | null;
  high_24h: number | null;
  low_24h: number | null;
  volume_24h: number | null;
  quote_volume_24h: number | null;
  open_interest: number | null;
  oi_change_1h_pct: number | null;
  liquidations_1h?: { long_liquidations_usdt: number; short_liquidations_usdt: number };
  forming_5m?: { open_time: number; open: number; high: number; low: number; close: number; volume: number } | null;
  ts: number;
  _updated_at?: string;
}

export interface Check {
  name: string;
  passed: boolean;
  detail: string;
}

export interface Setup {
  direction: "LONG" | "SHORT";
  entry: number;
  stop: number;
  tp1: number;
  tp2: number;
  rr: number;
  rr_tp2: number;
  confidence: number;
  level: number;
  reasons: string[];
  warnings: string[];
}

export interface TFSummary {
  interval: string;
  close: number | null;
  ema_fast: number | null;
  ema_mid: number | null;
  ema_slow: number | null;
  rsi: number | null;
  atr: number | null;
  atr_pct: number | null;
  vwap: number | null;
  volume_ratio: number | null;
  trend_score: number;
  trend: string;
}

export interface Evaluation {
  bar_time: number;
  price: number;
  decision: "LONG" | "SHORT" | "NO_TRADE";
  setup: Setup | null;
  long_checks: Check[];
  short_checks: Check[];
  summary: string;
  regime: string;
  timeframes: Record<string, TFSummary>;
  levels: { price: number; kind: string; touches: number; extreme: boolean }[];
  strategy_version?: number;
  evaluated_at?: string;
}

export interface TradingState {
  mode: Mode;
  kill_switch: boolean;
  strategy_enabled: boolean;
  halted: boolean;
  halt_reason: string | null;
  live_enabled_at?: string | null;
  kill_engaged_at?: string | null;
}

export interface EngineState {
  mode: Mode;
  guards: string[];
  halted: boolean;
  halt_reason: string | null;
  kill_switch: boolean;
  strategy_enabled: boolean;
  clock_skew_ms: number | null;
  market: { ws_connected: boolean; ws_public_connected: boolean; data_age_s: number | null; reconnects: number };
  version: string;
  uptime_s: number;
  credential_status: string;
  _updated_at?: string;
}

export interface Trade {
  id: number;
  mode: Mode;
  symbol: string;
  direction: "LONG" | "SHORT";
  status: string;
  origin: string;
  quantity: number;
  remaining_qty: number;
  leverage: number;
  entry_price: number | null;
  exit_price: number | null;
  stop_price: number | null;
  initial_stop: number | null;
  tp1_price: number | null;
  tp2_price: number | null;
  tp1_filled: boolean;
  liquidation_price: number | null;
  risk_usdt: number | null;
  equity_at_entry: number | null;
  opened_at: string | null;
  closed_at: string | null;
  exit_reason: string | null;
  fees: number;
  funding: number;
  gross_pnl: number;
  realized_pnl: number;
  pnl_pct: number | null;
  r_multiple: number | null;
  confidence: number | null;
  ai_decision: string | null;
  ai_confidence: number | null;
  market_regime: string | null;
  reason: string | null;
  reasons: string[];
  strategy_version: number | null;
  protection?: Record<string, unknown>;
  snapshot?: Record<string, unknown>;
}

export interface PositionRow {
  mode: Mode;
  symbol: string;
  direction: "LONG" | "SHORT" | null;
  quantity: number;
  entry_price: number | null;
  mark_price: number | null;
  unrealized_pnl: number;
  leverage: number | null;
  liquidation_price: number | null;
  margin: number | null;
  trade_id: number | null;
  updated_at: string;
}

export interface ParamSpec {
  key: string;
  group: string;
  label: string;
  type: "int" | "float" | "bool" | "enum" | "str" | "time";
  default: number | string | boolean;
  help: string;
  min: number | null;
  max: number | null;
  step: number | null;
  unit: string;
  options: string[];
  locked: boolean;
  risk_sensitive: boolean;
}

export interface ReadinessItem {
  key: string;
  label: string;
  ok: boolean;
  detail: string;
  how: string;
  required: boolean;
}
