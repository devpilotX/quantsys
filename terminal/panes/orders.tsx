import { useState } from "react";

import { Tabs, usePaneHeaderTabs } from "gloomberb/components";
import { colors } from "gloomberb/theme";
import type { PaneProps } from "gloomberb/types/plugin";
import { Box, ScrollBox, Text } from "gloomberb/ui";

import { formatInr, formatPrice, formatTs } from "../model";
import { QsBody, useQsFooter, useQsResource } from "../session";
import { QsTable, type QsColumn } from "../table";
import { PANE, type FillRow, type OrderRow } from "../types";

const VIEWS = [{ label: "Orders", value: "orders" }, { label: "Fills", value: "fills" }];

// livebroker.py WORKING_STATUSES; UNKNOWN is among them there but means in doubt, so it reads red here
const WORKING = new Set(["NEW", "PENDING_SUBMIT", "SUBMITTED", "PARTIAL"]);

function statusTone(status: string) {
  if (status === "FILLED") return "positive" as const;
  if (status === "REJECTED" || status === "UNKNOWN") return "negative" as const;
  if (WORKING.has(status)) return "warning" as const;
  return "muted" as const;
}

const ORDER_COLUMNS: QsColumn<OrderRow>[] = [
  { id: "ts", label: "TIME", width: 13, align: "left", text: (o) => formatTs(o.ts), sort: (o) => o.ts },
  { id: "symbol", label: "SYMBOL", width: 15, align: "left", text: (o) => o.symbol, sort: (o) => o.symbol },
  { id: "side", label: "SIDE", width: 5, align: "left", text: (o) => o.side,
    tone: (o) => (o.side === "SELL" ? "negative" : "positive") },
  { id: "qty", label: "QTY", width: 11, align: "right", text: (o) => `${o.filled_qty}/${o.qty}`, sort: (o) => o.qty },
  { id: "px", label: "PRICE", width: 11, align: "right", text: (o) => formatPrice(o.limit_price ?? o.ref_price) },
  { id: "style", label: "STYLE", width: 14, align: "left", text: (o) => o.style },
  { id: "status", label: "STATUS", width: 16, align: "left", text: (o) => o.status, sort: (o) => o.status,
    tone: (o) => statusTone(o.status) },
  { id: "strategy", label: "SLEEVE", width: 10, align: "left", flexGrow: 1, text: (o) => o.strategy },
];

const FILL_COLUMNS: QsColumn<FillRow>[] = [
  { id: "ts", label: "TIME", width: 13, align: "left", text: (f) => formatTs(f.ts), sort: (f) => f.ts },
  { id: "symbol", label: "SYMBOL", width: 15, align: "left", text: (f) => f.symbol, sort: (f) => f.symbol },
  { id: "qty", label: "QTY", width: 8, align: "right", text: (f) => (f.qty > 0 ? `+${f.qty}` : String(f.qty)),
    sort: (f) => f.qty, tone: (f) => (f.qty < 0 ? "negative" : "positive") },
  { id: "px", label: "PRICE", width: 11, align: "right", text: (f) => formatPrice(f.price) },
  { id: "notional", label: "NOTIONAL", width: 10, align: "right", text: (f) => formatInr(Math.abs(f.qty) * f.price),
    sort: (f) => Math.abs(f.qty) * f.price },
  { id: "fees", label: "FEES", width: 8, align: "right", text: (f) => formatInr(f.fees_total), sort: (f) => f.fees_total },
  { id: "bps", label: "BPS", width: 6, align: "right", sort: (f) => f.fees_total / (Math.abs(f.qty) * f.price || 1),
    text: (f) => (f.qty && f.price ? (f.fees_total / (Math.abs(f.qty) * f.price) * 1e4).toFixed(1) : "-") },
  { id: "strategy", label: "SLEEVE", width: 10, align: "left", flexGrow: 1, text: (f) => f.strategy },
];

function OrderDetail({ order }: { order: OrderRow }) {
  const lines = [
    `id ${order.client_order_id}`,
    `urgency ${order.urgency}   reference ${formatPrice(order.ref_price)}`,
    order.reason ? `reason ${order.reason}` : null,
    order.reject_reason ? `rejected: ${order.reject_reason}` : null,
  ].filter((l): l is string => !!l);
  return (
    <ScrollBox scrollY focusable={false} flexDirection="column" flexGrow={1} paddingX={1} paddingTop={1}>
      {lines.map((l, i) => <Box key={i}><Text fg={colors.text} wrapText>{l}</Text></Box>)}
    </ScrollBox>
  );
}

function FillDetail({ fill }: { fill: FillRow }) {
  const parts = Object.entries(fill.fees ?? {}).filter(([, v]) => typeof v === "number" && v !== 0);
  return (
    <ScrollBox scrollY focusable={false} flexDirection="column" flexGrow={1} paddingX={1} paddingTop={1}>
      <Box><Text fg={colors.textDim}>{`order ${fill.client_order_id ?? "-"}`}</Text></Box>
      {parts.map(([k, v]) => (
        <Box key={k}><Text fg={colors.text}>{`${k.padEnd(14)}${formatInr(v).padStart(10)}`}</Text></Box>
      ))}
      <Box><Text fg={colors.textBright}>{`${"total".padEnd(14)}${formatInr(fill.fees_total).padStart(10)}`}</Text></Box>
    </ScrollBox>
  );
}

export function OrdersPane({ width, height, focused }: PaneProps) {
  const [view, setView] = useState("orders");
  const orders = useQsResource<{ total: number; rows: OrderRow[] }>(view === "orders" ? "/orders?limit=500" : null);
  const fills = useQsResource<FillRow[]>(view === "fills" ? "/fills?limit=500" : null);
  const active = view === "orders" ? orders : fills;
  useQsFooter(PANE.orders, active);
  const inHeader = usePaneHeaderTabs({ tabs: VIEWS, activeValue: view, onSelect: setView, focused });
  const tabs = inHeader ? null : (
    <Tabs tabs={VIEWS} activeValue={view} onSelect={setView} focused={focused} variant="underline" dense />
  );
  const bodyHeight = Math.max(1, height - (inHeader ? 0 : 1));
  return (
    <Box flexDirection="column" width={width} height={height}>
      {tabs}
      <QsBody width={width} height={bodyHeight} loading={active.loading} error={active.error} hasData={!!active.data}>
        {view === "orders" ? (
          <QsTable items={orders.data?.rows ?? []} columns={ORDER_COLUMNS} width={width} height={bodyHeight}
            focused={focused} getKey={(o) => String(o.id)} defaultSort={{ columnId: "ts", direction: "desc" }}
            detail={(o) => <OrderDetail order={o} />} detailTitle={(o) => `${o.side} ${o.qty} ${o.symbol}`}
            emptyTitle="No orders." />
        ) : (
          <QsTable items={fills.data ?? []} columns={FILL_COLUMNS} width={width} height={bodyHeight}
            focused={focused} getKey={(f) => String(f.id)} defaultSort={{ columnId: "ts", direction: "desc" }}
            detail={(f) => <FillDetail fill={f} />} detailTitle={(f) => `${f.qty} ${f.symbol} @ ${formatPrice(f.price)}`}
            emptyTitle="No fills." />
        )}
      </QsBody>
    </Box>
  );
}
