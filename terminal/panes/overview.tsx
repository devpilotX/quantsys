import { StatGrid, type StatItem } from "gloomberb/components";
import { colors } from "gloomberb/theme";
import type { PaneProps } from "gloomberb/types/plugin";
import { Box, ScrollBox, Text, TextAttributes } from "gloomberb/ui";

import {
  bar, engineState, formatAge, formatInr, formatNum, formatPct, formatTs, leverage, regimeMix, signTone,
} from "../model";
import { QsBody, useQsFooter, useQsResource } from "../session";
import { QsTable, type QsColumn } from "../table";
import { PANE, type AlertRow, type Overview, type PnlMetrics, type RiskEventRow } from "../types";

/** A bold section label, the terminal equivalent of a panel title. */
export function Section({ title }: { title: string }) {
  return (
    <Box height={1} marginTop={1} paddingX={1}>
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

const EVENT_COLUMNS: QsColumn<RiskEventRow | AlertRow>[] = [
  { id: "ts", label: "TIME", width: 13, align: "left", text: (r) => formatTs(r.ts) },
  { id: "sev", label: "SEV", width: 5, align: "left", text: (r) => r.severity.toUpperCase().slice(0, 4),
    tone: (r) => (r.severity === "crit" || r.severity === "critical" ? "negative" : r.severity === "warn" ? "warning" : "muted") },
  { id: "kind", label: "EVENT", width: 22, align: "left", text: (r) => r.kind },
  { id: "what", label: "DETAIL", width: 20, align: "left", flexGrow: 1,
    text: (r) => ("title" in r ? r.title : [r.symbol, r.cause].filter(Boolean).join("  ")) },
];

export function OverviewPane({ width, height, focused }: PaneProps) {
  const overview = useQsResource<Overview>("/overview");
  const metrics = useQsResource<PnlMetrics>("/pnl/metrics", 60_000);
  useQsFooter(PANE.overview, overview);
  const o = overview.data;
  const m = metrics.data && !metrics.data.insufficient_data ? metrics.data : null;
  const events = o ? [...o.recent_risk_events, ...o.recent_alerts]
    .sort((a, b) => (b.ts ?? "").localeCompare(a.ts ?? "")) : [];

  return (
    <QsBody width={width} height={height} loading={overview.loading} error={overview.error} hasData={!!o}>
      {o ? (
        <ScrollBox scrollY focusable={false} flexDirection="column" width={width} height={height}>
          <Section title="Engine" />
          <StatGrid items={engineItems(o)} width={width} />
          <Section title="Book" />
          <StatGrid items={bookItems(o, m)} width={width} />
          <Section title="Risk" />
          <StatGrid items={riskItems(o, m)} width={width} />
          <Section title="Recent events" />
          <Box height={Math.max(4, Math.min(events.length + 2, 12))}>
            <QsTable items={events} columns={EVENT_COLUMNS} width={width}
              height={Math.max(4, Math.min(events.length + 2, 12))} focused={focused}
              getKey={(r) => ("title" in r ? `a${r.id}` : `r${r.id}`)} emptyTitle="No risk events or alerts." />
          </Box>
        </ScrollBox>
      ) : null}
    </QsBody>
  );
}
