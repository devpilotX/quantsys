import { describe, expect, test } from "bun:test";

import {
  auditByStage, backtestGate, bar, describeAudit, drawdownChartPoints, engineState, equityChartPoints,
  formatAge, formatInr, formatPct, formatTs, leverage, regimeMix,
} from "./model";
import { joinSleeves } from "./panes/strategies";
import type { Overview } from "./types";

function overview(patch: { status?: string; stale?: boolean; open?: boolean; halted?: boolean; kill?: string | null }): Overview {
  return {
    server_ts: "2026-09-27T10:00:00", mode: "paper", mode_requested: null,
    engine: { status: patch.status ?? "running", heartbeat_age_s: 4, stale: patch.stale ?? false,
      market_open: patch.open ?? true, host: "vps" },
    equity: { value: 1e6, ts: null, cash: null, gross_exposure: null, net_exposure: null,
      unrealized_pnl: null, today_pnl: null },
    open_positions: 0,
    decision: { id: 1, ts: "2026-09-27T09:45:00", tier: "T2", regime: "calm_trend", regime_probs: null,
      vol_scaler: 1, risk_frac_eff: 0.006, halted: patch.halted ?? false, kill_reason: patch.kill ?? null },
    recent_alerts: [], recent_risk_events: [],
  };
}

describe("formatting", () => {
  test("rupees in the Indian scale, sign only when asked", () => {
    expect(formatInr(12_34_567)).toBe("₹12.35L");
    expect(formatInr(2.5e7)).toBe("₹2.50cr");
    expect(formatInr(-4_200)).toBe("-₹4.2k");
    expect(formatInr(950, { signed: true })).toBe("+₹950");
    expect(formatInr(null)).toBe("-");
    expect(formatInr(Number.NaN)).toBe("-");
  });

  test("percentages and ages", () => {
    expect(formatPct(0.0625)).toBe("6.25%");
    expect(formatPct(0.01, 1, { signed: true })).toBe("+1.0%");
    expect(formatPct(undefined)).toBe("-");
    expect(formatAge(42)).toBe("42s");
    expect(formatAge(7_200)).toBe("2h");
    expect(formatAge(null)).toBe("never");
  });

  test("timestamps: time for today, date and time otherwise", () => {
    const now = new Date("2026-09-27T12:00:00");
    expect(formatTs("2026-09-27T09:35:00", now)).toBe("09:35");
    expect(formatTs("2026-09-26T15:15:00", now)).toBe("26 Sep 15:15");
    expect(formatTs(null, now)).toBe("-");
    expect(formatTs("garbage", now)).toBe("-");
  });

  test("bars clamp to their range", () => {
    expect(bar(0.5, 4)).toBe("██░░");
    expect(bar(2, 4)).toBe("████");
    expect(bar(-1, 4)).toBe("░░░░");
    expect(bar(null)).toBe("");
  });
});

describe("engineState", () => {
  test("a kill outranks a halt, which outranks a stale heartbeat", () => {
    expect(engineState(overview({ kill: "max drawdown", halted: true, stale: true })).label).toBe("KILLED");
    expect(engineState(overview({ halted: true, stale: true })).label).toBe("HALTED");
    expect(engineState(overview({ stale: true })).label).toBe("STALE");
  });

  test("the recorder's status strings", () => {
    expect(engineState(overview({ status: "running" })).label).toBe("RUNNING");
    expect(engineState(overview({ status: "running", open: false })).label).toBe("MARKET CLOSED");
    expect(engineState(overview({ status: "paused" })).label).toBe("PAUSED");
    expect(engineState(overview({ status: "halted" })).label).toBe("HALTED");
    expect(engineState(overview({ status: "stopped" })).label).toBe("STOPPED");
  });
});

describe("backtestGate", () => {
  const passing = { is_synthetic: false, sharpe_oos: 1.2, sharpe_deflated: 0.97, monte_carlo: { p_sharpe_negative: 0.04 } };

  test("passes only a real run that clears every threshold", () => {
    expect(backtestGate(passing)).toEqual({ pass: true, reasons: [] });
  });

  test("names each failed threshold", () => {
    const r = backtestGate({ ...passing, sharpe_oos: 0.6, sharpe_deflated: 0.5, monte_carlo: { p_sharpe_negative: 0.3 } });
    expect(r.pass).toBe(false);
    expect(r.reasons).toEqual(["OOS Sharpe 0.60 < 0.8", "deflated 0.50 < 0.95", "P(SR<0) 0.30 > 0.1"]);
  });

  test("synthetic or missing metrics never pass, as in livegate.py", () => {
    expect(backtestGate({ ...passing, is_synthetic: true }).reasons).toContain("synthetic data");
    expect(backtestGate({ ...passing, is_synthetic: undefined }).pass).toBe(false);
    expect(backtestGate({ ...passing, monte_carlo: null }).reasons).toContain("no P(SR<0)");
    expect(backtestGate(null).pass).toBe(false);
  });

  test("the thresholds are inclusive at the boundary", () => {
    expect(backtestGate({ ...passing, sharpe_oos: 0.8, sharpe_deflated: 0.95,
      monte_carlo: { p_sharpe_negative: 0.1 } }).pass).toBe(true);
  });
});

describe("audit trail", () => {
  const events = [
    { stage: "sizing", rule: "dust", detail: "g1", symbol: "SBIN", before: 3200, after: 0 },
    { stage: "risk.gross_cap", rule: "scale", detail: "", before: 0.9, after: 0.75 },
    { stage: "sizing", rule: "cost_gate", detail: "edge < cost" },
  ];

  test("counts per stage, largest first", () => {
    expect(auditByStage(events)).toEqual([{ stage: "sizing", count: 2 }, { stage: "risk.gross_cap", count: 1 }]);
    expect(auditByStage(null)).toEqual([]);
  });

  test("one line per event with its change", () => {
    expect(describeAudit(events[0]!)).toBe("dust SBIN 3200 -> 0  g1");
    expect(describeAudit(events[1]!)).toBe("scale 0.9000 -> 0.7500");
    expect(describeAudit(events[2]!)).toBe("cost_gate  edge < cost");
  });
});

describe("chart series", () => {
  const curve = [
    { ts: "2026-09-02T15:15:00", equity: 110, cash: null, gross_exposure: null, net_exposure: null },
    { ts: "2026-09-01T15:15:00", equity: 100, cash: null, gross_exposure: null, net_exposure: null },
    { ts: "2026-09-03T15:15:00", equity: 99, cash: null, gross_exposure: null, net_exposure: null },
    { ts: "bad", equity: 500, cash: null, gross_exposure: null, net_exposure: null },
  ];

  test("equity points are time-ordered and drop unparseable rows", () => {
    expect(equityChartPoints(curve).map((p) => p.close)).toEqual([100, 110, 99]);
  });

  test("drawdown is measured from the running peak", () => {
    const dd = drawdownChartPoints(equityChartPoints(curve)).map((p) => Number(p.close.toFixed(4)));
    expect(dd).toEqual([0, 0, -0.1]);
  });
});

describe("book figures", () => {
  test("leverage needs positive equity", () => {
    expect(leverage(1.5e6, 1e6)).toBe(1.5);
    expect(leverage(-2e6, 1e6)).toBe(2);
    expect(leverage(1e6, 0)).toBeNull();
    expect(leverage(null, 1e6)).toBeNull();
  });

  test("regime mix sorts and drops non-finite probabilities", () => {
    expect(regimeMix({ calm_range: 0.2, calm_trend: 0.7, turbulent: Number.NaN }))
      .toEqual([{ label: "calm_trend", p: 0.7 }, { label: "calm_range", p: 0.2 }]);
  });
});

describe("joinSleeves", () => {
  test("joins allocator statistics with booked P&L and keeps sleeves with P&L only", () => {
    const stats = [{ strategy: "trend", enabled: true, ts: null, mu: 1e-4, var: 1e-6, n_eff: 800,
      kelly_f: 0.1, sharpe_ann: 0.8, incubating: false }];
    const pnl = [
      { bucket: "trend", gross_pnl: 5_000, fees: 800, net_pnl: 4_200, n_trades: 12 },
      { bucket: "baseline", gross_pnl: 0, fees: 0, net_pnl: -100, n_trades: 1 },
    ];
    const rows = joinSleeves(stats, pnl);
    expect(rows.map((r) => [r.strategy, r.net_pnl, r.n_trades])).toEqual([["trend", 4_200, 12], ["baseline", -100, 1]]);
    expect(rows[1]!.enabled).toBe(false);
  });
});
