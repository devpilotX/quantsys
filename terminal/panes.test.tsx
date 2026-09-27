import { afterAll, afterEach, beforeAll, describe, expect, test } from "bun:test";
import { act, type ReactElement } from "react";

import {
  createInitialState, createTestPaneConfig, createTestPluginRuntime, PaneFooterBar, PaneFooterProvider, settleFrame,
  TestPaneProvider,
  testRender,
} from "gloomberb/test-support";
import { Box } from "gloomberb/ui";
import { setHttpFetchTransport } from "gloomberb/utils";

import { qsClient } from "./client";
import { FIXTURES } from "./fixtures";
import plugin from "./index";
import { PANE } from "./types";

/**
 * Each pane rendered through the host's own test harness against fixture
 * payloads, the way the app draws it. Catches a pane that throws on a real
 * payload, shows the wrong field, or never leaves its loading state.
 */

let setup: Awaited<ReturnType<typeof testRender>> | undefined;
const requested: string[] = [];

beforeAll(() => {
  setHttpFetchTransport(async (url) => {
    const path = new URL(url).pathname.replace(/^\/api/, "") + new URL(url).search;
    requested.push(path);
    if (path === "/auth/login") {
      const headers = new Headers({ "Content-Type": "application/json" });
      headers.append("Set-Cookie", "qs_session=test; HttpOnly; Path=/");
      return new Response("{}", { headers });
    }
    const body = FIXTURES[path];
    return body === undefined
      ? new Response(JSON.stringify({ detail: `no fixture for ${path}` }), { status: 404 })
      : new Response(JSON.stringify(body), { headers: { "Content-Type": "application/json" } });
  });
});

afterAll(() => {
  setHttpFetchTransport(null);
  qsClient.disconnect();
});

afterEach(async () => {
  if (setup) await act(async () => { setup!.renderer.destroy(); });
  setup = undefined;
});

function paneComponent(id: string) {
  const def = plugin.panes!.find((p) => p.id === id)!;
  return def.component as (props: { paneId: string; paneType: string; focused: boolean; width: number; height: number }) => ReactElement;
}

async function renderPane(id: string, width = 130, height = 44): Promise<string> {
  const Pane = paneComponent(id);
  const instanceId = `${id}:test`;
  const state = createInitialState(createTestPaneConfig("/tmp/gloom-quantsys", {
    instanceId, paneId: id, binding: { kind: "none" }, params: {},
  }));
  setup = await testRender(
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
  return setup.captureCharFrame();
}

describe("before login", () => {
  test("a pane asks to connect instead of fetching", async () => {
    requested.length = 0;
    const frame = await renderPane(PANE.overview);
    expect(frame).toContain("Not connected to quantsys.");
    expect(frame).toContain("QSC");
    expect(requested).toEqual([]);
  });
});

describe("connected", () => {
  beforeAll(async () => {
    await qsClient.login("http://127.0.0.1:8000", "op", "pw", "123456");
  });

  test("monitor: engine, book, risk and events", async () => {
    const frame = await renderPane(PANE.overview);
    for (const text of ["RUNNING", "PAPER", "calm_trend", "₹10.23L", "+₹3.5k", "T2", "0.84", "armed", "cap_bound"]) {
      expect(frame).toContain(text);
    }
  });

  test("positions: both legs with signed P&L", async () => {
    const frame = await renderPane(PANE.positions);
    for (const text of ["NIFTY-FUT", "SBIN", "-150", "+₹7.1k", "meanrev"]) expect(frame).toContain(text);
  });

  test("orders: the journal row", async () => {
    const frame = await renderPane(PANE.orders);
    for (const text of ["NIFTY-FUT", "65/65", "FILLED", "LIMIT_SMART"]) expect(frame).toContain(text);
  });

  test("decisions: the bar summary", async () => {
    const frame = await renderPane(PANE.decisions);
    for (const text of ["calm_trend", "₹10.23L", "0.58%"]) expect(frame).toContain(text);
  });

  test("sleeves: allocator statistics joined with P&L", async () => {
    const frame = await renderPane(PANE.strategies);
    for (const text of ["trend", "14.0%", "+₹16.9k", "incubating"]) expect(frame).toContain(text);
  });

  test("backtests: a real run that fails the gate", async () => {
    const frame = await renderPane(PANE.backtests);
    for (const text of ["replay nse15", "real", "0.41", "FAIL", "live gate: closed"]) expect(frame).toContain(text);
  });

  test("performance: metrics and attribution", async () => {
    const frame = await renderPane(PANE.performance);
    for (const text of ["+2.35%", "Sharpe", "0.84", "P&L BY REGIME", "calm_trend", "NIFTY-FUT"]) {
      expect(frame).toContain(text);
    }
  });

  test("risk: exposure, regime mix and the cap that bound", async () => {
    const frame = await renderPane(PANE.risk);
    for (const text of ["0.60x", "calm_trend", "71.0%", "risk.instrument_cap"]) expect(frame).toContain(text);
  });
});
