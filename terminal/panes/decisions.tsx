import { StatGrid } from "gloomberb/components";
import { colors } from "gloomberb/theme";
import type { PaneProps } from "gloomberb/types/plugin";
import { Box, ScrollBox, Text } from "gloomberb/ui";

import { auditByStage, describeAudit, formatInr, formatNum, formatPct, formatTs, regimeMix } from "../model";
import { QsBody, useQsFooter, useQsResource } from "../session";
import { QsTable, type QsColumn } from "../table";
import { PANE, type DecisionDetail, type DecisionSummary } from "../types";
import { Section } from "./overview";

const COLUMNS: QsColumn<DecisionSummary>[] = [
  { id: "ts", label: "BAR", width: 13, align: "left", text: (d) => formatTs(d.ts), sort: (d) => d.ts },
  { id: "equity", label: "EQUITY", width: 10, align: "right", text: (d) => formatInr(d.equity), sort: (d) => d.equity },
  { id: "tier", label: "TIER", width: 5, align: "left", text: (d) => d.tier },
  { id: "regime", label: "REGIME", width: 12, align: "left", text: (d) => d.regime, sort: (d) => d.regime },
  { id: "risk", label: "RISK", width: 7, align: "right", text: (d) => formatPct(d.risk_frac_eff, 2),
    sort: (d) => d.risk_frac_eff },
  { id: "vol", label: "VOLx", width: 6, align: "right", text: (d) => formatNum(d.vol_scaler) },
  { id: "sig", label: "SIG", width: 5, align: "right", text: (d) => String(d.n_signals), sort: (d) => d.n_signals },
  { id: "ord", label: "ORD", width: 5, align: "right", text: (d) => String(d.n_orders), sort: (d) => d.n_orders },
  { id: "aud", label: "AUDIT", width: 6, align: "right", text: (d) => String(d.n_audit), sort: (d) => d.n_audit },
  { id: "state", label: "STATE", width: 10, align: "left", flexGrow: 1,
    text: (d) => d.kill_reason ?? (d.halted ? "halted" : "ok"),
    tone: (d) => (d.kill_reason || d.halted ? "negative" : "muted") },
];

function line(text: string, color: string = colors.text) {
  return <Box paddingX={1}><Text fg={color} wrapText>{text}</Text></Box>;
}

function asText(value: unknown): string {
  if (value == null) return "-";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(4);
  return typeof value === "object" ? JSON.stringify(value) : String(value);
}

/** The full story of one decision bar: regime, Kelly, signals, targets, orders, audit. */
function DecisionView({ id, width }: { id: number; width: number }) {
  const detail = useQsResource<DecisionDetail>(`/decisions/${id}`, 300_000);
  const d = detail.data;
  if (!d) return line(detail.error ?? "Loading decision...", detail.error ? colors.warning : colors.textDim);
  const kelly = Object.entries(d.kelly ?? {}).sort(([, a], [, b]) => b - a);
  return (
    <ScrollBox scrollY focusable={false} flexDirection="column" flexGrow={1} width={width}>
      <StatGrid width={width} items={[
        { id: "equity", label: "Equity", value: formatInr(d.equity) },
        { id: "tier", label: "Tier", value: d.tier },
        { id: "risk", label: "Risk / trade", value: formatPct(d.risk_frac_eff, 3) },
        { id: "vol", label: "Vol scaler", value: formatNum(d.vol_scaler) },
        ...regimeMix(d.regime_probs).map((r) => ({ id: `p-${r.label}`, label: r.label, value: formatPct(r.p, 1) })),
      ]} />
      {kelly.length ? <Section title="Kelly allocation" /> : null}
      {kelly.map(([name, f]) => line(`${name.padEnd(12)}${formatPct(f, 2).padStart(8)}`))}
      <Section title={`Signals (${d.signals?.length ?? 0})`} />
      {(d.signals ?? []).map((s, i) => (
        <Box key={`s${i}`}>{line(`${asText(s.strategy).padEnd(10)} ${asText(s.symbol).padEnd(15)} dir ${asText(s.direction)}  `
          + `edge ${asText(s.expected_edge_R)}R  stop ${asText(s.stop_distance)}`)}</Box>
      ))}
      <Section title={`Targets (${d.targets?.length ?? 0})`} />
      {(d.targets ?? []).map((t, i) => (
        <Box key={`t${i}`}>{line(`${asText(t.symbol).padEnd(15)} ${asText(t.qty).padStart(8)}  ${asText(t.strategy)}`)}</Box>
      ))}
      <Section title={`Orders (${d.orders?.length ?? 0})`} />
      {(d.orders ?? []).map((o, i) => (
        <Box key={`o${i}`}>{line(`${asText(o.symbol).padEnd(15)} ${asText(o.qty_delta).padStart(8)}  `
          + `${asText(o.style)}  ${asText(o.urgency)}  ${asText(o.reason)}`)}</Box>
      ))}
      <Section title={`Audit trail (${d.audit?.length ?? 0})`} />
      {line(auditByStage(d.audit).map((s) => `${s.stage} ${s.count}`).join("   "), colors.textDim)}
      {(d.audit ?? []).map((a, i) => (
        <Box key={`a${i}`}>{line(`${a.stage.padEnd(14)} ${describeAudit(a)}`, colors.textDim)}</Box>
      ))}
    </ScrollBox>
  );
}

export function DecisionsPane({ width, height, focused }: PaneProps) {
  const decisions = useQsResource<{ total: number; rows: DecisionSummary[] }>("/decisions?limit=200");
  useQsFooter(PANE.decisions, decisions, decisions.data
    ? [{ id: "total", parts: [{ text: `${decisions.data.total} decisions`, tone: "muted" }] }] : []);
  return (
    <QsBody width={width} height={height} loading={decisions.loading} error={decisions.error} hasData={!!decisions.data}>
      <QsTable items={decisions.data?.rows ?? []} columns={COLUMNS} width={width} height={height}
        focused={focused} getKey={(d) => String(d.id)} defaultSort={{ columnId: "ts", direction: "desc" }}
        detail={(d) => <DecisionView id={d.id} width={width} />}
        detailTitle={(d) => `Decision #${d.id}  ${formatTs(d.ts)}`}
        emptyTitle="No decisions recorded yet." />
    </QsBody>
  );
}
