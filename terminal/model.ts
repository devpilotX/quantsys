import type { AuditEvent, BacktestMetrics, EquityPoint, Overview } from "./types";

/**
 * Pure derivations behind the panes: formatting, the live-gate check and the
 * series the charts draw. Nothing here fetches or renders, so all of it is
 * covered by model.test.ts.
 */

export const DASH = "-";

/** The live gate's backtest thresholds, as in qsdash/bridge/livegate.py. */
export const GATE = { minOosSharpe: 0.8, minDeflated: 0.95, maxPSharpeNegative: 0.1 } as const;

const finite = (value: number | null | undefined): value is number =>
  typeof value === "number" && Number.isFinite(value);

/** Rupees in the Indian scale the operator reads: 12.34L, 1.23cr. */
export function formatInr(value: number | null | undefined, { signed = false } = {}): string {
  if (!finite(value)) return DASH;
  const sign = value < 0 ? "-" : signed && value > 0 ? "+" : "";
  const abs = Math.abs(value);
  let body: string;
  if (abs >= 1e7) body = `${(abs / 1e7).toFixed(2)}cr`;
  else if (abs >= 1e5) body = `${(abs / 1e5).toFixed(2)}L`;
  else if (abs >= 1e3) body = `${(abs / 1e3).toFixed(1)}k`;
  else body = abs.toFixed(0);
  return `${sign}₹${body}`;
}

/** A price to two decimals, grouped the Indian way (1,23,456.70). */
export function formatPrice(value: number | null | undefined): string {
  if (!finite(value)) return DASH;
  return value.toLocaleString("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

export function formatPct(value: number | null | undefined, digits = 2, { signed = false } = {}): string {
  if (!finite(value)) return DASH;
  const text = `${(value * 100).toFixed(digits)}%`;
  return signed && value > 0 ? `+${text}` : text;
}

export function formatNum(value: number | null | undefined, digits = 2): string {
  return finite(value) ? value.toFixed(digits) : DASH;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** "09:35" for today, "26 Sep 09:35" otherwise; the server clock is IST. */
export function formatTs(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return DASH;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return DASH;
  const hhmm = date.toTimeString().slice(0, 5);
  if (date.toDateString() === now.toDateString()) return hhmm;
  // a fixed table: ICU month abbreviations differ by runtime ("Sep" / "Sept")
  return `${String(date.getDate()).padStart(2, "0")} ${MONTHS[date.getMonth()]} ${hhmm}`;
}

export function formatAge(seconds: number | null | undefined): string {
  if (!finite(seconds)) return "never";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86_400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86_400)}d`;
}

export type Tone = "neutral" | "positive" | "negative" | "warning" | "accent" | "muted";

export function signTone(value: number | null | undefined): Tone {
  if (!finite(value) || value === 0) return "neutral";
  return value > 0 ? "positive" : "negative";
}

/**
 * The engine's state in one word, most severe first: a kill or halt outranks
 * a stale heartbeat, which outranks the ordinary running states.
 */
export function engineState(overview: Overview): { label: string; tone: Tone } {
  const decision = overview.decision;
  const status = overview.engine.status.toLowerCase();
  if (decision?.kill_reason || status === "killed") return { label: "KILLED", tone: "negative" };
  if (decision?.halted || status === "halted") return { label: "HALTED", tone: "negative" };
  if (overview.engine.stale) return { label: "STALE", tone: "warning" };
  if (status === "paused") return { label: "PAUSED", tone: "warning" };
  if (status === "running") {
    return overview.engine.market_open
      ? { label: "RUNNING", tone: "positive" }
      : { label: "MARKET CLOSED", tone: "neutral" };
  }
  return { label: status ? status.toUpperCase() : "STOPPED", tone: "muted" };
}

/** Gross exposure as a multiple of equity, the figure the leverage cap binds on. */
export function leverage(gross: number | null | undefined, equity: number | null | undefined): number | null {
  if (!finite(gross) || !finite(equity) || equity <= 0) return null;
  return Math.abs(gross) / equity;
}

export interface GateResult {
  pass: boolean;
  reasons: string[];
}

/**
 * The same check livegate.backtest_gate() makes on one run: real data, and
 * every threshold present and met. A missing metric fails, as it does there.
 */
export function backtestGate(metrics: BacktestMetrics | null | undefined): GateResult {
  const m = metrics ?? {};
  const reasons: string[] = [];
  if (m.is_synthetic !== false) reasons.push("synthetic data");
  const sharpe = m.sharpe_oos;
  if (!finite(sharpe)) reasons.push("no OOS Sharpe");
  else if (sharpe < GATE.minOosSharpe) reasons.push(`OOS Sharpe ${sharpe.toFixed(2)} < ${GATE.minOosSharpe}`);
  const deflated = m.sharpe_deflated;
  if (!finite(deflated)) reasons.push("no deflated Sharpe");
  else if (deflated < GATE.minDeflated) reasons.push(`deflated ${deflated.toFixed(2)} < ${GATE.minDeflated}`);
  const pNeg = m.monte_carlo?.p_sharpe_negative;
  if (!finite(pNeg)) reasons.push("no P(SR<0)");
  else if (pNeg > GATE.maxPSharpeNegative) reasons.push(`P(SR<0) ${pNeg.toFixed(2)} > ${GATE.maxPSharpeNegative}`);
  return { pass: reasons.length === 0, reasons };
}

/** Audit events per pipeline stage, largest first, for the decision detail. */
export function auditByStage(events: readonly AuditEvent[] | null | undefined): Array<{ stage: string; count: number }> {
  const counts = new Map<string, number>();
  for (const event of events ?? []) counts.set(event.stage, (counts.get(event.stage) ?? 0) + 1);
  return [...counts.entries()]
    .map(([stage, count]) => ({ stage, count }))
    .sort((a, b) => b.count - a.count || a.stage.localeCompare(b.stage));
}

/** One line for an audit event: the rule, the change it made, and why. */
export function describeAudit(event: AuditEvent): string {
  const change = finite(event.before) && finite(event.after)
    ? ` ${formatCompactNumber(event.before)} -> ${formatCompactNumber(event.after)}`
    : "";
  const symbol = event.symbol ? ` ${event.symbol}` : "";
  return `${event.rule}${symbol}${change}${event.detail ? `  ${event.detail}` : ""}`;
}

function formatCompactNumber(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1e7) return `${(value / 1e7).toFixed(2)}cr`;
  if (abs >= 1e5) return `${(value / 1e5).toFixed(2)}L`;
  if (abs >= 100 || Number.isInteger(value)) return value.toFixed(0);
  return value.toFixed(4);
}

export interface ChartPoint {
  date: Date;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

/** Equity points in time order, as the chart surface expects them. */
export function equityChartPoints(points: readonly EquityPoint[]): ChartPoint[] {
  return points
    .filter((p) => finite(p.equity) && !Number.isNaN(new Date(p.ts).getTime()))
    .map((p) => ({ date: new Date(p.ts), open: p.equity, high: p.equity, low: p.equity, close: p.equity, volume: 0 }))
    .sort((a, b) => a.date.getTime() - b.date.getTime());
}

/** Drawdown from the running peak at each point, as a negative fraction. */
export function drawdownChartPoints(points: readonly ChartPoint[]): ChartPoint[] {
  let peak = -Infinity;
  return points.map((p) => {
    peak = Math.max(peak, p.close);
    const dd = peak > 0 ? p.close / peak - 1 : 0;
    return { ...p, open: dd, high: dd, low: dd, close: dd };
  });
}

/** Regime probabilities as sorted label/probability pairs. */
export function regimeMix(probs: Record<string, number> | null | undefined): Array<{ label: string; p: number }> {
  return Object.entries(probs ?? {})
    .filter(([, p]) => finite(p))
    .map(([label, p]) => ({ label, p }))
    .sort((a, b) => b.p - a.p);
}

/** A fixed-width bar for a fraction in [0, 1], for tables and stat values. */
export function bar(fraction: number | null | undefined, width = 10): string {
  if (!finite(fraction)) return "";
  const filled = Math.round(Math.min(1, Math.max(0, fraction)) * width);
  return "█".repeat(filled) + "░".repeat(width - filled);
}
