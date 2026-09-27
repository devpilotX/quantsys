import { useMemo, useState } from "react";

import { resolveChartPalette, StatGrid, StaticChartSurface, Tabs, usePaneHeaderTabs } from "gloomberb/components";
import { colors } from "gloomberb/theme";
import type { PaneProps } from "gloomberb/types/plugin";
import { Box, ScrollBox, Text } from "gloomberb/ui";

import {
  drawdownChartPoints, equityChartPoints, formatInr, formatNum, formatPct, signTone,
} from "../model";
import { QsBody, useQsFooter, useQsResource } from "../session";
import { PANE, type AttributionRow, type EquityPoint, type PnlMetrics } from "../types";
import { Section } from "./overview";

const VIEWS = [{ label: "Equity", value: "equity" }, { label: "Drawdown", value: "drawdown" }];

function AttributionLines({ rows, title }: { rows: AttributionRow[]; title: string }) {
  if (!rows.length) return null;
  const sorted = [...rows].sort((a, b) => b.net_pnl - a.net_pnl);
  return (
    <>
      <Section title={title} />
      {sorted.map((r) => (
        <Box key={r.bucket} paddingX={1}>
          <Text fg={r.net_pnl >= 0 ? colors.positive : colors.negative}>
            {`${r.bucket.padEnd(16)}${formatInr(r.net_pnl, { signed: true }).padStart(11)}`
              + `   fees ${formatInr(r.fees).padStart(8)}   ${String(r.n_trades).padStart(4)} trades`}
          </Text>
        </Box>
      ))}
    </>
  );
}

export function PerformancePane({ width, height, focused }: PaneProps) {
  const [view, setView] = useState("equity");
  const curve = useQsResource<EquityPoint[]>("/equity-curve?max_points=1500", 60_000);
  const metrics = useQsResource<PnlMetrics>("/pnl/metrics", 60_000);
  const byRegime = useQsResource<AttributionRow[]>("/pnl/attribution?by=regime", 120_000);
  const bySymbol = useQsResource<AttributionRow[]>("/pnl/attribution?by=symbol", 120_000);
  useQsFooter(PANE.performance, curve);
  const inHeader = usePaneHeaderTabs({ tabs: VIEWS, activeValue: view, onSelect: setView, focused });

  const equityPoints = useMemo(() => equityChartPoints(curve.data ?? []), [curve.data]);
  const points = useMemo(() => (view === "equity" ? equityPoints : drawdownChartPoints(equityPoints)),
    [view, equityPoints]);
  const m = metrics.data && !metrics.data.insufficient_data ? metrics.data : null;
  const trend = (m?.total_return ?? 0) >= 0 ? "positive" : "negative";
  const palette = useMemo(() => resolveChartPalette(colors, view === "drawdown" ? "negative" : trend),
    [view, trend]);
  const chartWidth = Math.max(24, width - 2);
  const chartHeight = Math.max(8, Math.min(18, Math.floor(height * 0.45)));

  return (
    <Box flexDirection="column" width={width} height={height}>
      {inHeader ? null : <Tabs tabs={VIEWS} activeValue={view} onSelect={setView} focused={focused} variant="underline" dense />}
      <QsBody width={width} height={height} loading={curve.loading} error={curve.error} hasData={!!curve.data}>
        <ScrollBox scrollY focusable={false} flexDirection="column" width={width}>
          {points.length >= 2 ? (
            <Box paddingX={1} marginTop={1}>
              <StaticChartSurface points={points} width={chartWidth} height={chartHeight} mode="line"
                colors={palette} showTimeAxis timeAxisColor={colors.textDim} yAxisColor={colors.textDim}
                formatYAxisValue={view === "equity" ? (v) => formatInr(v) : (v) => formatPct(v, 1)}
                focused={focused} />
            </Box>
          ) : (
            <Box paddingX={1} marginTop={1}><Text fg={colors.textMuted}>Not enough equity history to chart.</Text></Box>
          )}
          <Section title="Performance" />
          <StatGrid width={width} items={m ? [
            { id: "ret", label: "Total return", value: formatPct(m.total_return, 2, { signed: true }), tone: signTone(m.total_return) },
            { id: "cagr", label: "Annualized", value: formatPct(m.ann_return_est, 1, { signed: true }), tone: signTone(m.ann_return_est) },
            { id: "vol", label: "Volatility", value: formatPct(m.ann_vol_est, 1) },
            { id: "sharpe", label: "Sharpe", value: formatNum(m.sharpe) },
            { id: "sortino", label: "Sortino", value: formatNum(m.sortino) },
            { id: "calmar", label: "Calmar", value: formatNum(m.calmar) },
            { id: "dd", label: "Max drawdown", value: formatPct(m.max_drawdown), tone: "negative" },
            { id: "cdd", label: "Drawdown now", value: formatPct(m.current_drawdown) },
            { id: "hit", label: "Hit rate", value: formatPct(m.hit_rate, 1), detail: `${m.n_closed_trades ?? 0} trades` },
            { id: "pf", label: "Profit factor", value: formatNum(m.profit_factor) },
            { id: "fees", label: "Fees", value: formatInr(m.total_fees) },
            { id: "drag", label: "Cost drag", value: m.cost_drag_bps == null ? "-" : `${m.cost_drag_bps.toFixed(1)}bp` },
          ] : [{ id: "none", label: "Performance", value: "needs three days of equity", wide: true, tone: "muted" }]} />
          <AttributionLines rows={byRegime.data ?? []} title="P&L by regime" />
          <AttributionLines rows={bySymbol.data ?? []} title="P&L by symbol" />
        </ScrollBox>
      </QsBody>
    </Box>
  );
}
