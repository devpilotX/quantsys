import { StatGrid, statGridRows, type StatItem } from "gloomberb/components";
import { colors } from "gloomberb/theme";
import type { PaneProps } from "gloomberb/types/plugin";
import { Box, ScrollBox, Text, TextAttributes } from "gloomberb/ui";

import {
  bar, engineState, type Tone, formatAge, formatInr, formatNum, formatPct, formatTs, leverage, regimeMix, signTone,
} from "../model";
import { QsBody, useQsFooter, useQsResource } from "../session";
import { toneColor } from "../table";
import { PANE, type AlertRow, type Overview, type PnlMetrics, type RiskEventRow } from "../types";

/** A bold section label, the terminal equivalent of a panel title. */
export function Section({ title }: { title: string }) {
  return (
    <Box height={1} marginTop={1} paddingX={1} flexShrink={0}>
      <Text fg={colors.textBright} attributes={TextAttributes.BOLD}>{title.toUpperCase()}</Text>
    </Box>
  );
}

function engineItems(o: Overview): StatItem[] {
  const state = engineState(o);
  const d = o.decision;
  const topRegime = regimeMix(d?.regime_probs)[0];
  return [
    { id: "state", label: "Engine", value: state.label, tone: state.tone },
    { id: "mode", label: "Mode", value: o.mode.toUpperCase(),
      detail: o.mode_requested && o.mode_requested !== o.mode ? `-> ${o.mode_requested}` : undefined,
      tone: o.mode === "live" ? "warning" : "neutral" },
    { id: "hb", label: "Heartbeat", value: formatAge(o.engine.heartbeat_age_s),
      tone: o.engine.stale ? "warning" : "neutral" },
    { id: "bar", label: "Last decision", value: formatTs(d?.ts), detail: d ? `#${d.id}` : undefined },
    { id: "tier", label: "Tier", value: d?.tier ?? "-" },
    { id: "regime", label: "Regime", value: d?.regime ?? "-",
      detail: topRegime ? formatPct(topRegime.p, 0) : undefined },
  ];
}

function bookItems(o: Overview, m: PnlMetrics | null): StatItem[] {
  const e = o.equity;
  const lev = leverage(e.gross_exposure, e.value);
  return [
    { id: "equity", label: "Equity", value: formatInr(e.value), tone: "accent" },
    { id: "today", label: "Today", value: formatInr(e.today_pnl, { signed: true }), tone: signTone(e.today_pnl),
      detail: e.value && e.today_pnl != null ? formatPct(e.today_pnl / (e.value - e.today_pnl), 2, { signed: true }) : undefined },
    { id: "upnl", label: "Unrealized", value: formatInr(e.unrealized_pnl, { signed: true }), tone: signTone(e.unrealized_pnl) },
    { id: "cash", label: "Cash", value: formatInr(e.cash) },
    { id: "gross", label: "Gross", value: formatInr(e.gross_exposure), detail: lev != null ? `${lev.toFixed(2)}x` : undefined },
    { id: "net", label: "Net", value: formatInr(e.net_exposure, { signed: true }) },
    { id: "positions", label: "Positions", value: String(o.open_positions) },
    { id: "total", label: "Total return", value: formatPct(m?.total_return, 2, { signed: true }), tone: signTone(m?.total_return) },
  ];
}

function riskItems(o: Overview, m: PnlMetrics | null): StatItem[] {
  const d = o.decision;
  // 18% is the throttle's full-scale drawdown in config/base.yaml
  const dd = m?.current_drawdown;
  return [
    { id: "dd", label: "Drawdown", value: formatPct(dd), detail: bar(dd != null ? dd / 0.18 : null, 8),
      tone: dd != null && dd > 0.1 ? "warning" : "neutral" },
    { id: "maxdd", label: "Max drawdown", value: formatPct(m?.max_drawdown) },
    { id: "risk", label: "Risk / trade", value: formatPct(d?.risk_frac_eff, 3) },
    { id: "vol", label: "Vol scaler", value: formatNum(d?.vol_scaler) },
    { id: "sharpe", label: "Sharpe", value: formatNum(m?.sharpe) },
    { id: "kill", label: "Kill", value: d?.kill_reason ?? (d?.halted ? "halted" : "armed"),
      tone: d?.kill_reason || d?.halted ? "negative" : "positive", wide: !!d?.kill_reason },
  ];
}

const severityTone = (s: string): Tone => (s === "crit" ? "negative" : s === "warn" ? "warning" : "muted");

/** One recent risk event or alert as a fixed-column row: time, severity, kind, detail. */
function EventLine({ row, width }: { row: RiskEventRow | AlertRow; width: number }) {
  const detail = "title" in row ? row.title : [row.symbol, row.cause].filter(Boolean).join("  ");
  const tone = toneColor(severityTone(row.severity)) ?? colors.text;
  return (
    <Box height={1} paddingX={1} flexDirection="row" width={width} flexShrink={0}>
      <Text fg={colors.textDim}>{formatTs(row.ts).padEnd(14)}</Text>
      <Text fg={tone}>{row.severity.toUpperCase().padEnd(6)}</Text>
      <Text fg={colors.text}>{row.kind.padEnd(24)}</Text>
      <Text fg={colors.textDim}>{detail}</Text>
    </Box>
  );
}

export function OverviewPane({ width, height, focused }: PaneProps) {
  const overview = useQsResource<Overview>("/overview");
  const metrics = useQsResource<PnlMetrics>("/pnl/metrics", 60_000);
  useQsFooter(PANE.overview, overview);
  const o = overview.data;
  const m = metrics.data && !metrics.data.insufficient_data ? metrics.data : null;
  const events = o ? [...o.recent_risk_events, ...o.recent_alerts]
    .sort((a, b) => (b.ts ?? "").localeCompare(a.ts ?? "")) : [];

  const grid = (items: StatItem[]) => (
    // scroll content must not shrink, or yoga squeezes the grids and the rows below vanish
    <Box height={statGridRows(items, width)} flexShrink={0}><StatGrid items={items} width={width} /></Box>
  );

  return (
    <Box flexDirection="column" width={width} height={height}>
      <QsBody width={width} height={height} loading={overview.loading} error={overview.error} hasData={!!o}>
      {o ? (
        <ScrollBox scrollY focusable={false} flexDirection="column" flexGrow={1} width={width}>
          <Section title="Engine" />
          {grid(engineItems(o))}
          <Section title="Book" />
          {grid(bookItems(o, m))}
          <Section title="Risk" />
          {grid(riskItems(o, m))}
          <Section title="Recent events" />
          {events.length === 0
            ? <Box height={1} flexShrink={0} paddingX={1}><Text fg={colors.textMuted}>No risk events or alerts.</Text></Box>
            : events.map((row) => <EventLine key={"title" in row ? `a${row.id}` : `r${row.id}`} row={row} width={width} />)}
        </ScrollBox>
      ) : null}
      </QsBody>
    </Box>
  );
}
