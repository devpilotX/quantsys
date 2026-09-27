import { StatGrid } from "gloomberb/components";
import { colors } from "gloomberb/theme";
import type { PaneProps } from "gloomberb/types/plugin";
import { Box, ScrollBox, Text } from "gloomberb/ui";

import { backtestGate, formatNum, formatPct, formatTs, GATE } from "../model";
import { QsBody, useQsFooter, useQsResource } from "../session";
import { QsTable, type QsColumn } from "../table";
import { PANE, type BacktestRow } from "../types";
import { Section } from "./overview";

const COLUMNS: QsColumn<BacktestRow>[] = [
  { id: "created", label: "RUN", width: 13, align: "left", text: (b) => formatTs(b.created_at), sort: (b) => b.created_at },
  { id: "label", label: "LABEL", width: 24, align: "left", flexGrow: 1, text: (b) => b.label },
  { id: "data", label: "DATA", width: 9, align: "left", text: (b) => (b.metrics?.is_synthetic === false ? "real" : "synthetic"),
    tone: (b) => (b.metrics?.is_synthetic === false ? undefined : "muted") },
  { id: "sharpe", label: "OOS SR", width: 7, align: "right", text: (b) => formatNum(b.metrics?.sharpe_oos),
    sort: (b) => b.metrics?.sharpe_oos },
  { id: "dsr", label: "DEFL", width: 6, align: "right", text: (b) => formatNum(b.metrics?.sharpe_deflated),
    sort: (b) => b.metrics?.sharpe_deflated },
  { id: "pneg", label: "P(SR<0)", width: 8, align: "right", text: (b) => formatNum(b.metrics?.monte_carlo?.p_sharpe_negative),
    sort: (b) => b.metrics?.monte_carlo?.p_sharpe_negative },
  { id: "cagr", label: "CAGR", width: 8, align: "right", text: (b) => formatPct(b.metrics?.oos?.cagr, 1),
    sort: (b) => b.metrics?.oos?.cagr },
  { id: "dd", label: "MAX DD", width: 7, align: "right", text: (b) => formatPct(b.metrics?.max_dd, 1),
    sort: (b) => b.metrics?.max_dd },
  { id: "gate", label: "GATE", width: 6, align: "left", text: (b) => (backtestGate(b.metrics).pass ? "PASS" : "FAIL"),
    tone: (b) => (backtestGate(b.metrics).pass ? "positive" : "negative") },
];

function RunDetail({ run, width }: { run: BacktestRow; width: number }) {
  const m = run.metrics ?? {};
  const gate = backtestGate(m);
  return (
    <ScrollBox scrollY focusable={false} flexDirection="column" flexGrow={1} width={width}>
      <StatGrid width={width} items={[
        { id: "src", label: "Source", value: m.source ?? "-", detail: m.is_synthetic === false ? "real" : "synthetic" },
        { id: "rev", label: "Commit", value: run.git_rev ? run.git_rev.slice(0, 7) : "-" },
        { id: "trials", label: "Trials", value: m.n_trials == null ? "-" : String(m.n_trials) },
        { id: "trades", label: "OOS trades", value: m.oos?.n_trades == null ? "-" : String(m.oos.n_trades) },
        { id: "sr", label: "OOS Sharpe", value: formatNum(m.sharpe_oos), detail: `need ${GATE.minOosSharpe}` },
        { id: "dsr", label: "Deflated", value: formatNum(m.sharpe_deflated), detail: `need ${GATE.minDeflated}` },
        { id: "pneg", label: "P(SR<0)", value: formatNum(m.monte_carlo?.p_sharpe_negative), detail: `max ${GATE.maxPSharpeNegative}` },
        { id: "gate", label: "Live gate", value: gate.pass ? "PASS" : "FAIL", tone: gate.pass ? "positive" : "negative" },
      ]} />
      {gate.reasons.length ? <Section title="Why the gate refuses it" /> : null}
      {gate.reasons.map((r) => <Box key={r} paddingX={1}><Text fg={colors.negative}>{r}</Text></Box>)}
      {m.verdict ? <Section title="Verdict" /> : null}
      {m.verdict ? <Box paddingX={1}><Text fg={colors.text} wrapText>{m.verdict}</Text></Box> : null}
    </ScrollBox>
  );
}

export function BacktestsPane({ width, height, focused }: PaneProps) {
  const runs = useQsResource<BacktestRow[]>("/backtests?limit=200", 300_000);
  const anyPass = (runs.data ?? []).some((b) => backtestGate(b.metrics).pass);
  useQsFooter(PANE.backtests, runs, runs.data ? [{ id: "gate", parts: [{
    text: anyPass ? "live gate: a run passes" : "live gate: closed", tone: anyPass ? "positive" : "muted" }] }] : []);
  return (
    <QsBody width={width} height={height} loading={runs.loading} error={runs.error} hasData={!!runs.data}>
      <QsTable items={runs.data ?? []} columns={COLUMNS} width={width} height={height} focused={focused}
        getKey={(b) => String(b.id)} defaultSort={{ columnId: "created", direction: "desc" }}
        detail={(b) => <RunDetail run={b} width={width} />} detailTitle={(b) => b.label}
        emptyTitle="No backtest runs. Persist one with runstudy --persist." />
    </QsBody>
  );
}
