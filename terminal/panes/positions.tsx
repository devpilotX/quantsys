import { useState } from "react";

import { StatGrid, Tabs, usePaneHeaderTabs } from "gloomberb/components";
import { colors } from "gloomberb/theme";
import type { PaneProps } from "gloomberb/types/plugin";
import { Box, ScrollBox, Text } from "gloomberb/ui";

import { describeAudit, formatInr, formatPrice, formatTs, signTone } from "../model";
import { QsBody, useQsFooter, useQsResource } from "../session";
import { QsTable, type QsColumn } from "../table";
import { PANE, type AuditEvent, type PositionRow } from "../types";
import { Section } from "./overview";

interface Explain {
  entry: { rationale: Record<string, unknown> | null; decision: ExplainDecision | null };
  exit: { rationale: Record<string, unknown> | null; decision: ExplainDecision | null };
  fills: Array<{ ts: string | null; qty: number; price: number; fees_total: number }>;
}

interface ExplainDecision {
  id: number;
  ts: string | null;
  tier: string;
  regime: string;
  risk_frac_eff: number | null;
  audit_for_symbol: AuditEvent[];
}

const VIEWS = [{ label: "Open", value: "open" }, { label: "Closed", value: "closed" }];

const COLUMNS: QsColumn<PositionRow>[] = [
  { id: "symbol", label: "SYMBOL", width: 15, align: "left", text: (p) => p.symbol, sort: (p) => p.symbol },
  { id: "strategy", label: "SLEEVE", width: 10, align: "left", text: (p) => p.strategy, sort: (p) => p.strategy },
  { id: "qty", label: "QTY", width: 8, align: "right", text: (p) => String(p.qty), sort: (p) => p.qty,
    tone: (p) => (p.qty < 0 ? "negative" : undefined) },
  { id: "avg", label: "AVG", width: 11, align: "right", text: (p) => formatPrice(p.avg_price) },
  { id: "last", label: "LAST", width: 11, align: "right", text: (p) => formatPrice(p.last_price) },
  { id: "upnl", label: "UNREAL", width: 10, align: "right", text: (p) => formatInr(p.unrealized_pnl, { signed: true }),
    sort: (p) => p.unrealized_pnl, tone: (p) => signTone(p.unrealized_pnl) },
  { id: "rpnl", label: "REAL", width: 10, align: "right", text: (p) => formatInr(p.realized_pnl, { signed: true }),
    sort: (p) => p.realized_pnl, tone: (p) => signTone(p.realized_pnl) },
  { id: "fees", label: "FEES", width: 8, align: "right", text: (p) => formatInr(p.fees_paid), sort: (p) => p.fees_paid },
  { id: "opened", label: "OPENED", width: 13, align: "left", text: (p) => formatTs(p.opened_at),
    sort: (p) => p.opened_at },
];

function rationaleLines(rationale: Record<string, unknown> | null): string[] {
  return Object.entries(rationale ?? {}).map(([k, v]) => `${k}: ${typeof v === "object" ? JSON.stringify(v) : String(v)}`);
}

function DecisionStory({ label, side, width }: {
  label: string;
  side: Explain["entry"];
  width: number;
}) {
  const d = side.decision;
  return (
    <Box flexDirection="column">
      <Section title={label} />
      {d ? (
        <StatGrid width={width} items={[
          { id: "ts", label: "Decision", value: formatTs(d.ts), detail: `#${d.id}` },
          { id: "tier", label: "Tier", value: d.tier },
          { id: "regime", label: "Regime", value: d.regime },
        ]} />
      ) : null}
      {[...rationaleLines(side.rationale), ...(d?.audit_for_symbol ?? []).map(describeAudit)].map((line, i) => (
        <Box key={i} paddingX={1}><Text fg={colors.textDim} wrapText>{line}</Text></Box>
      ))}
      {!d && !side.rationale ? <Box paddingX={1}><Text fg={colors.textMuted}>No record.</Text></Box> : null}
    </Box>
  );
}

function PositionDetail({ position, width }: { position: PositionRow; width: number }) {
  const explain = useQsResource<Explain>(`/trades/${position.id}/explain`, 60_000);
  const e = explain.data;
  return (
    <ScrollBox scrollY focusable={false} flexDirection="column" flexGrow={1} width={width}>
      <StatGrid width={width} items={[
        { id: "qty", label: "Quantity", value: String(position.qty) },
        { id: "avg", label: "Average", value: formatPrice(position.avg_price) },
        { id: "stop", label: "Stop distance", value: formatPrice(position.stop_distance) },
        { id: "real", label: "Realized", value: formatInr(position.realized_pnl, { signed: true }),
          tone: signTone(position.realized_pnl) },
      ]} />
      {explain.error ? <Box paddingX={1}><Text fg={colors.warning}>{explain.error}</Text></Box> : null}
      {e ? (
        <>
          <DecisionStory label="Why it entered" side={e.entry} width={width} />
          {position.status === "closed" ? <DecisionStory label="Why it exited" side={e.exit} width={width} /> : null}
          <Section title="Fills" />
          {e.fills.map((f, i) => (
            <Box key={i} paddingX={1}>
              <Text fg={colors.text}>
                {`${formatTs(f.ts)}  ${f.qty > 0 ? "+" : ""}${f.qty} @ ${formatPrice(f.price)}  fees ${formatInr(f.fees_total)}`}
              </Text>
            </Box>
          ))}
        </>
      ) : null}
    </ScrollBox>
  );
}

export function PositionsPane({ width, height, focused }: PaneProps) {
  const [view, setView] = useState("open");
  const positions = useQsResource<PositionRow[]>(`/positions?status=${view}&limit=500`);
  useQsFooter(PANE.positions, positions);
  const inHeader = usePaneHeaderTabs({ tabs: VIEWS, activeValue: view, onSelect: setView, focused });
  const tabs = inHeader ? null : (
    <Tabs tabs={VIEWS} activeValue={view} onSelect={setView} focused={focused} variant="underline" dense />
  );
  const bodyHeight = Math.max(1, height - (inHeader ? 0 : 1));
  return (
    <Box flexDirection="column" width={width} height={height}>
      {tabs}
      <QsBody width={width} height={bodyHeight} loading={positions.loading} error={positions.error}
        hasData={!!positions.data}>
        <QsTable items={positions.data ?? []} columns={COLUMNS} width={width} height={bodyHeight}
          focused={focused} getKey={(p) => String(p.id)}
          defaultSort={view === "open" ? { columnId: "upnl", direction: "desc" } : { columnId: "opened", direction: "desc" }}
          detail={(p) => <PositionDetail position={p} width={width} />}
          detailTitle={(p) => `${p.symbol}  ${p.strategy}`}
          emptyTitle={view === "open" ? "No open positions." : "No closed positions."} />
      </QsBody>
    </Box>
  );
}
