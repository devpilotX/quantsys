/**
 * Shapes of the qsdash read API (dashboard/backend/qsdash/api/data.py), limited
 * to the fields the panes use. Every number the engine could not produce is
 * nullable on the server, so it is nullable here too.
 */

export const QS_PLUGIN_ID = "quantsys";

export const PANE = {
  overview: "quantsys-overview",
  positions: "quantsys-positions",
  orders: "quantsys-orders",
  decisions: "quantsys-decisions",
  strategies: "quantsys-strategies",
  performance: "quantsys-performance",
  risk: "quantsys-risk",
  backtests: "quantsys-backtests",
} as const;

export interface EngineStatus {
  status: string;
  heartbeat_age_s: number | null;
  stale: boolean;
  market_open: boolean;
  host: string;
}

export interface Overview {
  server_ts: string;
  mode: string;
  mode_requested: string | null;
  engine: EngineStatus;
  equity: {
    value: number | null;
    ts: string | null;
    cash: number | null;
    gross_exposure: number | null;
    net_exposure: number | null;
    unrealized_pnl: number | null;
    today_pnl: number | null;
  };
  open_positions: number;
  decision: {
    id: number;
    ts: string;
    tier: string;
    regime: string;
    regime_probs: Record<string, number> | null;
    vol_scaler: number | null;
    risk_frac_eff: number | null;
    halted: boolean;
    kill_reason: string | null;
  } | null;
  recent_alerts: AlertRow[];
  recent_risk_events: RiskEventRow[];
}

export interface PositionRow {
  id: number;
  symbol: string;
  strategy: string;
  qty: number;
  avg_price: number;
  last_price: number | null;
  unrealized_pnl: number | null;
  realized_pnl: number;
  fees_paid: number;
  stop_distance: number | null;
  status: string;
  opened_at: string | null;
  closed_at: string | null;
}

export interface OrderRow {
  id: number;
  client_order_id: string;
  ts: string | null;
  strategy: string;
  symbol: string;
  side: string;
  qty: number;
  filled_qty: number;
  style: string;
  urgency: string;
  limit_price: number | null;
  ref_price: number | null;
  status: string;
  reject_reason: string | null;
  reason: string | null;
}

export interface FillRow {
  id: number;
  ts: string | null;
  symbol: string;
  strategy: string;
  qty: number;
  price: number;
  fees_total: number;
  fees: Record<string, number> | null;
  slippage: number | null;
  client_order_id: string | null;
}

export interface DecisionSummary {
  id: number;
  ts: string;
  equity: number;
  tier: string;
  regime: string;
  vol_scaler: number | null;
  risk_frac_eff: number | null;
  halted: boolean;
  kill_reason: string | null;
  n_signals: number;
  n_orders: number;
  n_audit: number;
}

export interface AuditEvent {
  stage: string;
  rule: string;
  detail: string;
  symbol?: string | null;
  before?: number | null;
  after?: number | null;
}

export interface DecisionDetail extends Omit<DecisionSummary, "n_signals" | "n_orders" | "n_audit"> {
  regime_probs: Record<string, number> | null;
  kelly: Record<string, number> | null;
  signals: Array<Record<string, unknown>> | null;
  targets: Array<Record<string, unknown>> | null;
  orders: Array<Record<string, unknown>> | null;
  audit: AuditEvent[] | null;
}

export interface StrategyRow {
  strategy: string;
  enabled: boolean;
  ts: string | null;
  mu: number | null;
  var: number | null;
  n_eff: number | null;
  kelly_f: number | null;
  sharpe_ann: number | null;
  incubating: boolean;
}

export interface AttributionRow {
  bucket: string;
  gross_pnl: number;
  fees: number;
  net_pnl: number;
  n_trades: number;
}

export interface EquityPoint {
  ts: string;
  equity: number;
  cash: number | null;
  gross_exposure: number | null;
  net_exposure: number | null;
}

export interface PnlMetrics {
  insufficient_data: boolean;
  n_days?: number;
  max_drawdown?: number;
  current_drawdown?: number;
  total_return?: number | null;
  sharpe?: number | null;
  sortino?: number | null;
  calmar?: number | null;
  ann_return_est?: number;
  ann_vol_est?: number;
  n_closed_trades?: number;
  hit_rate?: number | null;
  profit_factor?: number | null;
  total_fees?: number;
  cost_drag_bps?: number | null;
}

export interface RegimeCurrent {
  available: boolean;
  ts?: string;
  label?: string;
  probs?: Record<string, number>;
  risk_scaler?: number;
  source?: string;
}

export interface RiskLimits {
  decision_id: number | null;
  decision_ts: string | null;
  halted: boolean;
  kill_reason: string | null;
  risk_frac_eff: number | null;
  vol_scaler: number | null;
  gross_exposure: number | null;
  net_exposure: number | null;
  equity: number | null;
  audit_events: AuditEvent[];
}

export interface RiskEventRow {
  id: number;
  ts: string | null;
  kind: string;
  rule: string | null;
  symbol: string | null;
  severity: string;
  cause: string | null;
}

export interface AlertRow {
  id: number;
  ts: string | null;
  severity: string;
  kind: string;
  title: string;
  delivered: boolean | null;
}

export interface BacktestMetrics {
  source?: string;
  is_synthetic?: boolean;
  sharpe_oos?: number | null;
  sharpe_deflated?: number | null;
  max_dd?: number | null;
  n_trials?: number | null;
  verdict?: string | null;
  oos?: { cagr?: number | null; n_trades?: number | null; hit_rate?: number | null } | null;
  monte_carlo?: { p_sharpe_negative?: number | null } | null;
}

export interface BacktestRow {
  id: number;
  created_at: string | null;
  label: string;
  git_rev: string;
  metrics: BacktestMetrics | null;
}
