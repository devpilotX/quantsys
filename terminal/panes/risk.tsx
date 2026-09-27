import { useState } from "react";

import { StatGrid, Tabs, usePaneHeaderTabs } from "gloomberb/components";
import { colors } from "gloomberb/theme";
import type { PaneProps } from "gloomberb/types/plugin";
import { Box, ScrollBox, Text } from "gloomberb/ui";

import { bar, describeAudit, formatInr, formatNum, formatPct, formatTs, leverage, regimeMix } from "../model";
import { QsBody, useQsFooter, useQsResource } from "../session";
import { QsTable, type QsColumn } from "../table";
import { PANE, type RegimeCurrent, type RiskEventRow, type RiskLimits } from "../types";
import { Section } from "./overview";

const VIEWS = [{ label: "Limits", value: "limits" }, { label: "Events", value: "events" }];

const EVENT_COLUMNS: QsColumn<RiskEventRow>[] = [
  { id: "ts", label: "TIME", width: 13, align: "left", text: (r) => formatTs(r.ts), sort: (r) => r.ts },
  { id: "sev", label: "SEV", width: 5, align: "left", text: (r) => r.severity.toUpperCase(), sort: (r) => r.severity,
    tone: (r) => (r.severity === "crit" ? "negative" : r.severity === "warn" ? "warning" : "muted") },
  { id: "kind", label: "EVENT", width: 24, align: "left", text: (r) => r.kind, sort: (r) => r.kind },
  { id: "rule", label: "RULE", width: 14, align: "left", text: (r) => r.rule ?? "-" },
  { id: "symbol", label: "SYMBOL", width: 14, align: "left", text: (r) => r.symbol ?? "-", sort: (r) => r.symbol },
  { id: "cause", label: "CAUSE", width: 20, align: "left", flexGrow: 1, text: (r) => r.cause ?? "" },
];

function LimitsView({ width }: { width: number }) {
  const limits = useQsResource<RiskLimits>("/risk/limits");
  const regime = useQsResource<RegimeCurrent>("/regime/current", 60_000);
  const l = limits.data;
  const r = regime.data?.available ? regime.data : null;
  if (!l) return <Box paddingX={1}><Text fg={colors.textDim}>{limits.error ?? "Loading limits..."}</Text></Box>;
  const lev = leverage(l.gross_exposure, l.equity);
  return (
    <ScrollBox scrollY focusable={false} flexDirection="column" flexGrow={1} width={width}>
      <Section title="Book" />
      <StatGrid width={width} items={[
        { id: "equity", label: "Equity", value: formatInr(l.equity) },
        { id: "gross", label: "Gross", value: formatInr(l.gross_exposure), detail: lev != null ? `${lev.toFixed(2)}x` : undefined },
        { id: "net", label: "Net", value: formatInr(l.net_exposure, { signed: true }) },
        { id: "risk", label: "Risk / trade", value: formatPct(l.risk_frac_eff, 3) },
        { id: "vol", label: "Vol scaler", value: formatNum(l.vol_scaler) },
        { id: "state", label: "Kill", value: l.kill_reason ?? (l.halted ? "halted" : "armed"),
          tone: l.kill_reason || l.halted ? "negative" : "positive" },
      ]} />
      {r ? (
        <>
          <Section title={`Regime  ${r.label ?? ""}  (${r.source ?? "-"})`} />
          {regimeMix(r.probs).map((p) => (
            <Box key={p.label} paddingX={1}>
              <Text fg={p.label === r.label ? colors.textBright : colors.textDim}>
                {`${p.label.padEnd(12)}${bar(p.p, 20)} ${formatPct(p.p, 1).padStart(7)}`}
              </Text>
            </Box>
          ))}
          <Box paddingX={1}><Text fg={colors.textDim}>{`risk scaler ${formatNum(r.risk_scaler)}`}</Text></Box>
        </>
      ) : null}
      <Section title={`Last bar: caps and scalings (${l.audit_events.length})`} />
      {l.audit_events.length === 0
        ? <Box paddingX={1}><Text fg={colors.textMuted}>No cap bound on the last decision.</Text></Box>
        : l.audit_events.map((a, i) => (
          <Box key={i} paddingX={1}>
            <Text fg={a.stage.startsWith("risk") ? colors.warning : colors.textDim} wrapText>
              {`${a.stage.padEnd(16)}${describeAudit(a)}`}
            </Text>
          </Box>
        ))}
    </ScrollBox>
  );
}

export function RiskPane({ width, height, focused }: PaneProps) {
  const [view, setView] = useState("limits");
  const events = useQsResource<RiskEventRow[]>(view === "events" ? "/risk/events?limit=500" : null);
  const probe = useQsResource<RiskLimits>(view === "limits" ? "/risk/limits" : null);
  const active = view === "events" ? events : probe;
  useQsFooter(PANE.risk, active);
  const inHeader = usePaneHeaderTabs({ tabs: VIEWS, activeValue: view, onSelect: setView, focused });
  const bodyHeight = Math.max(1, height - (inHeader ? 0 : 1));
  return (
    <Box flexDirection="column" width={width} height={height}>
      {inHeader ? null : <Tabs tabs={VIEWS} activeValue={view} onSelect={setView} focused={focused} variant="underline" dense />}
      <QsBody width={width} height={bodyHeight} loading={active.loading} error={active.error} hasData={!!active.data}>
        {view === "limits" ? <LimitsView width={width} /> : (
          <QsTable items={events.data ?? []} columns={EVENT_COLUMNS} width={width} height={bodyHeight}
            focused={focused} getKey={(r) => String(r.id)} defaultSort={{ columnId: "ts", direction: "desc" }}
            emptyTitle="No risk events." />
        )}
      </QsBody>
    </Box>
  );
}
