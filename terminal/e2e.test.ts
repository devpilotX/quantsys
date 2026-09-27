import { describe, expect, test } from "bun:test";

import { QsClient } from "./client";
import type {
  AttributionRow, BacktestRow, DecisionDetail, DecisionSummary, EquityPoint, FillRow, OrderRow, Overview,
  PnlMetrics, PositionRow, RegimeCurrent, RiskEventRow, RiskLimits, StrategyRow,
} from "./types";

/**
 * Against a running qsdash, skipped unless QS_E2E_URL is set:
 *
 *   QS_E2E_URL=http://127.0.0.1:8000 QS_E2E_USER=... QS_E2E_PASSWORD=... QS_E2E_TOTP=123456 bun test e2e
 *
 * It reads every endpoint a pane reads and checks the fields the panes use
 * are there with the right types, so a server change that would blank a
 * column fails here rather than on the operator's screen.
 */

const url = process.env.QS_E2E_URL;
const live = url ? describe : describe.skip;

function hasKeys(row: object, keys: string[]): void {
  for (const key of keys) expect(row).toHaveProperty(key);
}

live("qsdash end to end", () => {
  const client = new QsClient(globalThis.fetch);

  test("login", async () => {
    await client.login(url!, process.env.QS_E2E_USER ?? "", process.env.QS_E2E_PASSWORD ?? "",
      process.env.QS_E2E_TOTP ?? "");
    expect(client.getState().connected).toBe(true);
  });

  test("overview and metrics", async () => {
    const o = await client.get<Overview>("/overview");
    hasKeys(o, ["mode", "engine", "equity", "open_positions", "decision", "recent_alerts", "recent_risk_events"]);
    hasKeys(o.engine, ["status", "heartbeat_age_s", "stale", "market_open"]);
    expect(typeof o.equity.value).toBe("number");
    expect(o.decision).not.toBeNull();
    const m = await client.get<PnlMetrics>("/pnl/metrics");
    expect(typeof m.insufficient_data).toBe("boolean");
  });

  test("positions, orders and fills", async () => {
    for (const status of ["open", "closed"]) {
      const rows = await client.get<PositionRow[]>(`/positions?status=${status}&limit=500`);
      for (const p of rows) hasKeys(p, ["id", "symbol", "strategy", "qty", "avg_price", "realized_pnl", "fees_paid"]);
    }
    const orders = await client.get<{ total: number; rows: OrderRow[] }>("/orders?limit=500");
    for (const o of orders.rows) hasKeys(o, ["symbol", "side", "qty", "filled_qty", "style", "status"]);
    const fills = await client.get<FillRow[]>("/fills?limit=500");
    expect(fills.length).toBeGreaterThan(0);
    for (const f of fills) hasKeys(f, ["symbol", "qty", "price", "fees_total", "fees"]);
  });

  test("a closed trade explains itself", async () => {
    const closed = await client.get<PositionRow[]>("/positions?status=closed&limit=5");
    if (!closed.length) return;
    const e = await client.get<{ entry: object; exit: object; fills: unknown[] }>(`/trades/${closed[0]!.id}/explain`);
    hasKeys(e, ["entry", "exit", "fills"]);
  });

  test("decisions and one decision's detail", async () => {
    const d = await client.get<{ total: number; rows: DecisionSummary[] }>("/decisions?limit=200");
    expect(d.total).toBeGreaterThan(0);
    const detail = await client.get<DecisionDetail>(`/decisions/${d.rows[0]!.id}`);
    hasKeys(detail, ["signals", "targets", "orders", "audit", "kelly", "regime_probs"]);
    for (const a of detail.audit ?? []) hasKeys(a, ["stage", "rule", "detail"]);
  });

  test("sleeves, attribution and equity", async () => {
    const s = await client.get<StrategyRow[]>("/strategies");
    for (const r of s) hasKeys(r, ["strategy", "enabled", "kelly_f", "sharpe_ann", "n_eff", "incubating"]);
    for (const by of ["strategy", "regime", "symbol"]) {
      const rows = await client.get<AttributionRow[]>(`/pnl/attribution?by=${by}`);
      for (const r of rows) hasKeys(r, ["bucket", "net_pnl", "fees", "n_trades"]);
    }
    const curve = await client.get<EquityPoint[]>("/equity-curve?max_points=1500");
    expect(curve.length).toBeGreaterThan(1);
  });

  test("risk and regime", async () => {
    const l = await client.get<RiskLimits>("/risk/limits");
    hasKeys(l, ["halted", "risk_frac_eff", "vol_scaler", "gross_exposure", "audit_events"]);
    const r = await client.get<RegimeCurrent>("/regime/current");
    expect(typeof r.available).toBe("boolean");
    const events = await client.get<RiskEventRow[]>("/risk/events?limit=500");
    for (const e of events) hasKeys(e, ["kind", "severity"]);
  });

  test("backtests carry the metrics the gate reads", async () => {
    const runs = await client.get<BacktestRow[]>("/backtests?limit=200");
    for (const b of runs) {
      expect(b.metrics).not.toBeNull();
      hasKeys(b.metrics!, ["is_synthetic", "sharpe_oos", "sharpe_deflated", "monte_carlo"]);
    }
  });
});
