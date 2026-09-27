import { useMemo } from "react";

import type { PaneProps } from "gloomberb/types/plugin";
import { Box } from "gloomberb/ui";

import { formatInr, formatNum, formatPct, formatTs, signTone } from "../model";
import { QsBody, useQsFooter, useQsResource } from "../session";
import { QsTable, type QsColumn } from "../table";
import { PANE, type AttributionRow, type StrategyRow } from "../types";

/** One sleeve: the allocator's live statistics joined with its booked P&L. */
interface SleeveRow extends StrategyRow {
  net_pnl: number | null;
  fees: number | null;
  n_trades: number | null;
}

const COLUMNS: QsColumn<SleeveRow>[] = [
  { id: "strategy", label: "SLEEVE", width: 11, align: "left", text: (s) => s.strategy, sort: (s) => s.strategy },
  { id: "state", label: "STATE", width: 10, align: "left",
    text: (s) => (!s.enabled ? "off" : s.incubating ? "incubating" : "on"),
    tone: (s) => (!s.enabled ? "muted" : s.incubating ? "warning" : "positive") },
  { id: "kelly", label: "KELLY f", width: 8, align: "right", text: (s) => formatPct(s.kelly_f, 1), sort: (s) => s.kelly_f },
  { id: "sharpe", label: "SHARPE", width: 7, align: "right", text: (s) => formatNum(s.sharpe_ann),
    sort: (s) => s.sharpe_ann, tone: (s) => signTone(s.sharpe_ann) },
  { id: "mu", label: "MU/BAR", width: 9, align: "right", text: (s) => (s.mu == null ? "-" : `${(s.mu * 1e4).toFixed(2)}bp`),
    sort: (s) => s.mu, tone: (s) => signTone(s.mu) },
  { id: "neff", label: "N EFF", width: 7, align: "right", text: (s) => (s.n_eff == null ? "-" : s.n_eff.toFixed(0)),
    sort: (s) => s.n_eff },
  { id: "net", label: "NET P&L", width: 10, align: "right", text: (s) => formatInr(s.net_pnl, { signed: true }),
    sort: (s) => s.net_pnl, tone: (s) => signTone(s.net_pnl) },
  { id: "fees", label: "FEES", width: 8, align: "right", text: (s) => formatInr(s.fees), sort: (s) => s.fees },
  { id: "trades", label: "TRADES", width: 7, align: "right", text: (s) => (s.n_trades == null ? "-" : String(s.n_trades)),
    sort: (s) => s.n_trades },
  { id: "ts", label: "UPDATED", width: 13, align: "left", flexGrow: 1, text: (s) => formatTs(s.ts) },
];

export function joinSleeves(stats: readonly StrategyRow[], pnl: readonly AttributionRow[]): SleeveRow[] {
  const byName = new Map(pnl.map((p) => [p.bucket, p]));
  const rows: SleeveRow[] = stats.map((s) => {
    const p = byName.get(s.strategy);
    byName.delete(s.strategy);
    return { ...s, net_pnl: p?.net_pnl ?? null, fees: p?.fees ?? null, n_trades: p?.n_trades ?? null };
  });
  // P&L booked by a sleeve that has no allocator row (the baseline book, say)
  for (const p of byName.values()) {
    rows.push({ strategy: p.bucket, enabled: false, ts: null, mu: null, var: null, n_eff: null, kelly_f: null,
      sharpe_ann: null, incubating: false, net_pnl: p.net_pnl, fees: p.fees, n_trades: p.n_trades });
  }
  return rows;
}

export function StrategiesPane({ width, height, focused }: PaneProps) {
  const stats = useQsResource<StrategyRow[]>("/strategies");
  const pnl = useQsResource<AttributionRow[]>("/pnl/attribution?by=strategy", 60_000);
  useQsFooter(PANE.strategies, stats);
  const rows = useMemo(() => joinSleeves(stats.data ?? [], pnl.data ?? []), [stats.data, pnl.data]);
  return (
    <Box flexDirection="column" width={width} height={height}>
      <QsBody width={width} height={height} loading={stats.loading} error={stats.error} hasData={!!stats.data}>
        <QsTable items={rows} columns={COLUMNS} width={width} height={height} focused={focused}
          getKey={(s) => s.strategy} defaultSort={{ columnId: "kelly", direction: "desc" }}
          emptyTitle="No sleeve statistics yet." />
      </QsBody>
    </Box>
  );
}
