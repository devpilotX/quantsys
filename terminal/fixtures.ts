/**
 * Payloads shaped exactly as qsdash returns them (dashboard/backend/qsdash/api/data.py),
 * for the pane render tests. Values are a small paper book on the 15-minute clock.
 */

const TODAY = "2026-09-27";

export const FIXTURES: Record<string, unknown> = {
  "/overview": {
    server_ts: `${TODAY}T11:00:04`, mode: "paper", mode_requested: null,
    engine: { status: "running", heartbeat_age_s: 4.2, stale: false, market_open: true, host: "vps", detail: {} },
    equity: { value: 1_023_456.7, ts: `${TODAY}T11:00:00`, cash: 412_000, gross_exposure: 611_456.7,
      net_exposure: 188_000, unrealized_pnl: 8_200.5, today_pnl: 3_456.7 },
    open_positions: 2,
    decision: { id: 812, ts: `${TODAY}T11:00:00`, tier: "T2", regime: "calm_trend",
      regime_probs: { calm_trend: 0.71, calm_range: 0.22, turbulent: 0.07 },
      vol_scaler: 0.92, risk_frac_eff: 0.0058, halted: false, kill_reason: null },
    capital_controls: { paper_capital: 1_000_000, deployable_cap_frac: null, deployable_cap_abs: null },
    recent_alerts: [{ id: 3, ts: `${TODAY}T09:16:00`, severity: "info", kind: "engine_start",
      title: "paper engine started", delivered: true }],
    recent_risk_events: [{ id: 9, ts: `${TODAY}T10:45:00`, kind: "cap_bound", rule: "per_instrument",
      severity: "warn", cause: "NIFTY-FUT above 25% of equity", symbol: "NIFTY-FUT" }],
  },
  "/pnl/metrics": {
    insufficient_data: false, n_days: 60, max_drawdown: 0.041, current_drawdown: 0.012, total_return: 0.0235,
    sharpe: 0.84, sortino: 1.1, calmar: 2.3, ann_return_est: 0.094, ann_vol_est: 0.11, n_closed_trades: 38,
    hit_rate: 0.42, profit_factor: 1.18, total_fees: 14_250.4, turnover_notional: 9.1e7, cost_drag_bps: 1.6,
  },
  "/positions?status=open&limit=500": [
    { id: 41, mode: "paper", symbol: "NIFTY-FUT", strategy: "trend", qty: 65, avg_price: 25_010.5,
      last_price: 25_120, unrealized_pnl: 7_117.5, realized_pnl: 0, fees_paid: 312.4, stop_distance: 180,
      status: "open", opened_at: `${TODAY}T09:45:00`, closed_at: null },
    { id: 42, mode: "paper", symbol: "SBIN", strategy: "meanrev", qty: -150, avg_price: 812.4,
      last_price: 805.2, unrealized_pnl: 1_080, realized_pnl: 0, fees_paid: 95.1, stop_distance: 14,
      status: "open", opened_at: `${TODAY}T10:15:00`, closed_at: null },
  ],
  "/orders?limit=500": {
    total: 1, rows: [{ id: 7, client_order_id: "QD2609270945012-s0", ts: `${TODAY}T09:45:00`, decision_id: 800,
      strategy: "trend", symbol: "NIFTY-FUT", side: "BUY", qty: 65, filled_qty: 65, style: "LIMIT_SMART",
      urgency: "NORMAL", limit_price: 25_015, ref_price: 25_010.5, status: "FILLED", broker_order_id: null,
      reject_reason: null, reason: "trend entry", status_history: [] }],
  },
  "/decisions?limit=200": {
    total: 812, rows: [{ id: 812, ts: `${TODAY}T11:00:00`, equity: 1_023_456.7, tier: "T2", regime: "calm_trend",
      regime_source: "hmm", vol_scaler: 0.92, risk_frac_eff: 0.0058, halted: false, kill_reason: null,
      n_signals: 4, n_orders: 1, n_audit: 11 }],
  },
  "/strategies": [
    { strategy: "trend", enabled: true, ts: `${TODAY}T11:00:00`, mu: 0.00012, var: 2e-6, n_eff: 910,
      kelly_f: 0.14, sharpe_ann: 0.9, incubating: false },
    { strategy: "tom", enabled: true, ts: `${TODAY}T11:00:00`, mu: null, var: null, n_eff: 12,
      kelly_f: 0.08, sharpe_ann: null, incubating: true },
  ],
  "/pnl/attribution?by=strategy": [
    { bucket: "trend", gross_pnl: 21_000, fees: 4_100, net_pnl: 16_900, n_trades: 22 },
  ],
  "/backtests?limit=200": [
    { id: 5, created_at: `${TODAY}T08:00:00`, label: "replay nse15 2026-09-26", git_rev: "a40680e1",
      metrics: { source: "replay", is_synthetic: false, sharpe_oos: 0.41, sharpe_deflated: 0.12, max_dd: 0.09,
        n_trials: 30, oos: { cagr: 0.031, n_trades: 140 }, monte_carlo: { p_sharpe_negative: 0.27 } } },
  ],
  "/equity-curve?max_points=1500": Array.from({ length: 40 }, (_, i) => ({
    ts: new Date(Date.UTC(2026, 7, 1 + i, 9, 45)).toISOString(), equity: 1_000_000 + i * 900 - (i % 7) * 1_500,
    cash: null, gross_exposure: null, net_exposure: null, realized_pnl: null, unrealized_pnl: null,
  })),
  "/pnl/attribution?by=regime": [{ bucket: "calm_trend", gross_pnl: 18_000, fees: 3_000, net_pnl: 15_000, n_trades: 18 }],
  "/pnl/attribution?by=symbol": [{ bucket: "NIFTY-FUT", gross_pnl: 12_000, fees: 1_900, net_pnl: 10_100, n_trades: 9 }],
  "/risk/limits": {
    decision_id: 812, decision_ts: `${TODAY}T11:00:00`, halted: false, kill_reason: null, risk_frac_eff: 0.0058,
    vol_scaler: 0.92, gross_exposure: 611_456.7, net_exposure: 188_000, equity: 1_023_456.7, config_overrides: {},
    audit_events: [{ stage: "risk.instrument_cap", rule: "scale", detail: "NIFTY-FUT", symbol: "NIFTY-FUT",
      before: 290_000, after: 255_000 }],
  },
  "/regime/current": { available: true, ts: `${TODAY}T11:00:00`, label: "calm_trend",
    probs: { calm_trend: 0.71, calm_range: 0.22, turbulent: 0.07 }, risk_scaler: 1, source: "hmm" },
};
