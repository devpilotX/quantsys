/**
 * Renders quantsys panes to SVG for the README, through Gloomberb's own
 * renderer and against demo payloads, so the images show exactly what the
 * terminal draws.
 *
 *   bun scripts/screenshots.tsx ../docs/images
 */
import { mkdirSync, writeFileSync } from "fs";
import { join } from "path";
import { act, type ReactElement } from "react";

import {
  createInitialState, createTestPaneConfig, createTestPluginRuntime, emitKeypress, PaneFooterBar,
  PaneFooterProvider, settleFrame, TestPaneProvider, testRender,
} from "gloomberb/test-support";
import { colors } from "gloomberb/theme";
import { Box } from "gloomberb/ui";
import { setHttpFetchTransport } from "gloomberb/utils";

import { qsClient } from "../client";
import { FIXTURES } from "../fixtures";
import plugin from "../index";
import { PANE } from "../types";

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const DAY = "2026-09-27";
const SYMBOLS = ["NIFTY-FUT", "BANKNIFTY-FUT", "RELIANCE", "HDFCBANK", "INFY", "SBIN", "TCS", "ICICIBANK"];

/** A fuller book than the test fixtures, so the images look like a working day. */
const DEMO: Record<string, unknown> = {
  ...FIXTURES,
  "/positions?status=open&limit=500": SYMBOLS.slice(0, 6).map((symbol, i) => ({
    id: 40 + i, mode: "paper", symbol, strategy: ["trend", "tom", "factor", "factor", "meanrev", "meanrev"][i],
    qty: [65, 30, 120, 90, -140, -150][i], avg_price: [25_010.5, 55_880, 2_941.2, 1_702.4, 1_512.9, 812.4][i],
    last_price: [25_120, 56_040, 2_968.5, 1_694.1, 1_498.3, 805.2][i],
    unrealized_pnl: [7_117.5, 4_800, 3_276, -747, 2_044, 1_080][i], realized_pnl: 0,
    fees_paid: [312.4, 298.1, 96.4, 71.2, 88.3, 95.1][i], stop_distance: [180, 420, 61, 38, 30, 14][i],
    status: "open", opened_at: `${DAY}T${["09:45", "09:30", "10:00", "10:00", "10:15", "10:15"][i]}:00`, closed_at: null,
  })),
  "/strategies": ["trend", "meanrev", "factor", "expiry", "downshock", "tom"].map((strategy, i) => ({
    strategy, enabled: true, ts: `${DAY}T11:00:00`, mu: [1.2e-4, 3e-5, 6e-5, -2e-5, 9e-5, 1.4e-4][i],
    var: 2e-6, n_eff: [910, 640, 402, 188, 96, 12][i], kelly_f: [0.14, 0.06, 0.09, 0.02, 0.05, 0.08][i],
    sharpe_ann: [0.9, 0.31, 0.52, -0.2, 0.61, null][i], incubating: i === 5,
  })),
  "/decisions/812": {
    id: 812, ts: `${DAY}T11:00:00`, mode: "paper", equity: 1_023_456.7, tier: "T2", regime: "calm_trend",
    regime_probs: { calm_trend: 0.71, calm_range: 0.22, turbulent: 0.07 }, regime_source: "hmm",
    risk_frac_eff: 0.0058, vol_scaler: 0.92, halted: false, kill_reason: null,
    kelly: { trend: 0.14, factor: 0.09, tom: 0.08, meanrev: 0.06 },
    signals: [
      { strategy: "trend", symbol: "NIFTY-FUT", direction: 0.62, expected_edge_R: 0.12, stop_distance: 180 },
      { strategy: "tom", symbol: "BANKNIFTY-FUT", direction: 1, expected_edge_R: 0.15, stop_distance: 420 },
      { strategy: "meanrev", symbol: "SBIN", direction: -0.8, expected_edge_R: 0.1, stop_distance: 14 },
      { strategy: "factor", symbol: "RELIANCE", direction: 0.4, expected_edge_R: 0.1, stop_distance: 61 },
    ],
    targets: [
      { symbol: "NIFTY-FUT", qty: 65, strategy: "trend" }, { symbol: "BANKNIFTY-FUT", qty: 30, strategy: "tom" },
      { symbol: "SBIN", qty: -150, strategy: "meanrev" }, { symbol: "RELIANCE", qty: 120, strategy: "factor" },
    ],
    orders: [{ symbol: "RELIANCE", qty_delta: 30, style: "LIMIT_SINGLE", urgency: "NORMAL", reason: "rebalance" }],
    audit: [
      { stage: "regime", rule: "risk_scaler", detail: "calm_trend", before: 1, after: 1 },
      { stage: "vol_target", rule: "scale", detail: "book vol 14.1% vs 13%", before: 1, after: 0.92 },
      { stage: "risk.instrument_cap", rule: "scale", detail: "25% of equity", symbol: "NIFTY-FUT", before: 290_000, after: 255_000 },
      { stage: "sizing", rule: "hedge_ratio_drift", detail: "meanrev:SBIN|PNB", before: 0.031, after: 0.1 },
      { stage: "sizing", rule: "cost_gate", detail: "expiry:INFY edge Rs410 < 3.0x cost Rs180" },
      { stage: "orders", rule: "band", detail: "HDFCBANK delta inside 30% band", symbol: "HDFCBANK" },
    ],
  },
};

setHttpFetchTransport(async (url) => {
  const u = new URL(url);
  const path = u.pathname.replace(/^\/api/, "") + u.search;
  if (path === "/auth/login") {
    const headers = new Headers({ "Content-Type": "application/json" });
    headers.append("Set-Cookie", "qs_session=demo; HttpOnly; Path=/");
    return new Response("{}", { headers });
  }
  const body = DEMO[path];
  return body === undefined
    ? new Response(JSON.stringify({ detail: `no demo data for ${path}` }), { status: 404 })
    : new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
});

interface Span { text: string; fg: string; bg: string | null; bold: boolean; width: number }

function hex(rgba: { toInts(): [number, number, number, number] }): string | null {
  const [r, g, b, a] = rgba.toInts();
  if (a === 0) return null;
  return `#${[r, g, b].map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

const escapeXml = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");

/** One pane as an SVG terminal window: a title bar, then the captured cells. */
function toSvg(title: string, mnemonic: string, lines: Span[][], cols: number): string {
  const cw = 8.4, ch = 18, pad = 14, bar = 30;
  const w = Math.round(cols * cw + pad * 2), h = Math.round(lines.length * ch + pad * 2 + bar);
  const bg = colors.bg, panel = colors.panel ?? colors.bg;
  const out: string[] = [
    `<svg xmlns="http://www.w3.org/2000/svg" width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" role="img" aria-label="${escapeXml(title)}">`,
    `<rect width="${w}" height="${h}" rx="8" fill="${bg}"/>`,
    `<rect width="${w}" height="${bar}" rx="8" fill="${panel}"/><rect y="${bar - 8}" width="${w}" height="8" fill="${panel}"/>`,
    ...["#ff5f57", "#febc2e", "#28c840"].map((c, i) => `<circle cx="${pad + 6 + i * 18}" cy="${bar / 2}" r="5.5" fill="${c}"/>`),
    `<text x="${w / 2}" y="${bar / 2 + 4.5}" fill="${colors.textDim}" font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,monospace" font-size="12" text-anchor="middle">${escapeXml(`${mnemonic}  ${title}`)}</text>`,
    `<g font-family="ui-monospace,SFMono-Regular,Menlo,Consolas,'Liberation Mono',monospace" font-size="14" xml:space="preserve">`,
  ];
  lines.forEach((spans, row) => {
    let col = 0;
    const y = pad + bar + row * ch;
    for (const s of spans) {
      const x = pad + col * cw;
      if (s.bg && s.bg !== bg) out.push(`<rect x="${x.toFixed(1)}" y="${y}" width="${(s.width * cw).toFixed(1)}" height="${ch}" fill="${s.bg}"/>`);
      // each run of non-space cells is placed on its own columns, so padding
      // never stretches a glyph and every column lines up as in the terminal
      const cells = Array.from(s.text);
      for (let i = 0; i < cells.length;) {
        if (cells[i] === " ") { i++; continue; }
        let j = i;
        while (j < cells.length && cells[j] !== " ") j++;
        const run = cells.slice(i, j).join("");
        out.push(`<text x="${(x + i * cw).toFixed(1)}" y="${y + 13.5}" fill="${s.fg}"${s.bold ? ' font-weight="700"' : ""} textLength="${((j - i) * cw).toFixed(1)}" lengthAdjust="spacingAndGlyphs">${escapeXml(run)}</text>`);
        i = j;
      }
      col += s.width;
    }
  });
  out.push("</g></svg>");
  return out.join("\n");
}

async function shoot(id: string, file: string, opts: { width?: number; height?: number; open?: boolean } = {}) {
  const width = opts.width ?? 124, height = opts.height ?? 30;
  const def = plugin.panes!.find((p) => p.id === id)!;
  const template = plugin.paneTemplates!.find((t) => t.paneId === id)!;
  const Pane = def.component as (p: { paneId: string; paneType: string; focused: boolean; width: number; height: number }) => ReactElement;
  const instanceId = `${id}:shot`;
  const state = createInitialState(createTestPaneConfig("/tmp/gloom-quantsys-shots", {
    instanceId, paneId: id, binding: { kind: "none" }, params: {},
  }));
  const setup = await testRender(
    <TestPaneProvider state={state} paneId={instanceId} pluginId="quantsys" runtime={createTestPluginRuntime()}>
      <PaneFooterProvider>{(footer) => (
        <Box flexDirection="column" width={width} height={height}>
          <Pane paneId={instanceId} paneType={id} focused width={width} height={height - 1} />
          <PaneFooterBar footer={footer} focused width={width} />
        </Box>
      )}</PaneFooterProvider>
    </TestPaneProvider>,
    { width, height },
  );
  for (let i = 0; i < 12; i++) await settleFrame(setup);
  if (opts.open) {
    await emitKeypress(setup, { name: "return" });
    for (let i = 0; i < 12; i++) await settleFrame(setup);
  }
  const frame = setup.captureSpans();
  const lines: Span[][] = frame.lines.map((l) => l.spans.map((s) => ({
    text: s.text, fg: hex(s.fg) ?? colors.text, bg: hex(s.bg), bold: (s.attributes & 1) === 1, width: s.width,
  })));
  // trim trailing blank rows so each image ends at its content
  while (lines.length && lines[lines.length - 1]!.every((s) => !s.text.trim() && !s.bg)) lines.pop();
  writeFileSync(file, toSvg(def.name, typeof template.shortcut === "object" ? template.shortcut.prefix : "", lines, width));
  await act(async () => { setup.renderer.destroy(); });
  console.log(`wrote ${file}`);
}

const outDir = process.argv[2] ?? "screenshots";
mkdirSync(outDir, { recursive: true });
await qsClient.login("http://127.0.0.1:8000", "demo", "demo", "000000");
await shoot(PANE.overview, join(outDir, "terminal-monitor.svg"), { height: 21 });
await shoot(PANE.positions, join(outDir, "terminal-positions.svg"), { height: 11 });
await shoot(PANE.decisions, join(outDir, "terminal-decision.svg"), { height: 36, open: true });
await shoot(PANE.strategies, join(outDir, "terminal-sleeves.svg"), { height: 11 });
await shoot(PANE.performance, join(outDir, "terminal-performance.svg"), { height: 34 });
process.exit(0);
