import type { GloomPlugin, PaneDef, PaneTemplateDef } from "gloomberb/types/plugin";

import { qsClient } from "./client";
import { BacktestsPane } from "./panes/backtests";
import { DecisionsPane } from "./panes/decisions";
import { OrdersPane } from "./panes/orders";
import { OverviewPane } from "./panes/overview";
import { PerformancePane } from "./panes/performance";
import { PositionsPane } from "./panes/positions";
import { RiskPane } from "./panes/risk";
import { StrategiesPane } from "./panes/strategies";
import { PANE, QS_PLUGIN_ID } from "./types";

/**
 * quantsys in Gloomberb: a read-only monitor of the quantsys decision engine,
 * reading the qsdash API. Each pane has a command-bar mnemonic in the
 * Bloomberg style (QS, QSP, QSO, ...).
 */

interface PaneSpec {
  id: string;
  name: string;
  icon: string;
  shortcut: string;
  description: string;
  keywords: string[];
  component: PaneDef["component"];
  size: { width: number; height: number };
}

const PANES: PaneSpec[] = [
  { id: PANE.overview, name: "quantsys Monitor", icon: "Q", shortcut: "QS", component: OverviewPane,
    description: "Engine state, equity, drawdown, tier, regime and the latest risk events.",
    keywords: ["quantsys", "engine", "monitor", "overview", "equity"], size: { width: 120, height: 40 } },
  { id: PANE.positions, name: "quantsys Positions", icon: "P", shortcut: "QSP", component: PositionsPane,
    description: "Open and closed positions with P&L, and why each one entered and exited.",
    keywords: ["quantsys", "positions", "book", "pnl"], size: { width: 120, height: 34 } },
  { id: PANE.orders, name: "quantsys Orders", icon: "O", shortcut: "QSO", component: OrdersPane,
    description: "The order journal and every fill with its fee breakdown.",
    keywords: ["quantsys", "orders", "fills", "blotter", "executions"], size: { width: 120, height: 34 } },
  { id: PANE.decisions, name: "quantsys Decisions", icon: "D", shortcut: "QSD", component: DecisionsPane,
    description: "Every decision bar: signals, Kelly weights, targets, orders and the full audit trail.",
    keywords: ["quantsys", "decisions", "audit", "signals", "explain"], size: { width: 120, height: 40 } },
  { id: PANE.strategies, name: "quantsys Sleeves", icon: "S", shortcut: "QSS", component: StrategiesPane,
    description: "Each strategy sleeve's Kelly fraction, edge statistics and booked P&L.",
    keywords: ["quantsys", "strategies", "sleeves", "kelly", "attribution"], size: { width: 120, height: 24 } },
  { id: PANE.performance, name: "quantsys Performance", icon: "E", shortcut: "QSE", component: PerformancePane,
    description: "Equity and drawdown curves, Sharpe, costs and P&L by regime and symbol.",
    keywords: ["quantsys", "performance", "equity", "drawdown", "sharpe"], size: { width: 120, height: 44 } },
  { id: PANE.risk, name: "quantsys Risk", icon: "R", shortcut: "QSR", component: RiskPane,
    description: "Exposure, the regime mix, the caps that bound on the last bar, and risk events.",
    keywords: ["quantsys", "risk", "limits", "caps", "regime", "kill"], size: { width: 120, height: 40 } },
  { id: PANE.backtests, name: "quantsys Backtests", icon: "B", shortcut: "QSB", component: BacktestsPane,
    description: "Walk-forward runs, their deflated Sharpe and whether each clears the live gate.",
    keywords: ["quantsys", "backtest", "walk-forward", "gate", "deflated"], size: { width: 120, height: 30 } },
];

export const quantsysPlugin: GloomPlugin = {
  id: QS_PLUGIN_ID,
  name: "quantsys",
  version: "0.1.0",
  description: "Read-only monitor for the quantsys NSE trading engine",
  homepage: "https://github.com/devpilotX/qi",
  toggleable: true,
  // Terminal only: the session cookie is read from the login response, which
  // the desktop and web transports do not pass through.
  targets: ["cli", "tui"],

  configSchema: [
    { key: "baseUrl", label: "qsdash URL", type: "text", defaultValue: "http://127.0.0.1:8000",
      description: "The dashboard API. Plain http only for localhost." },
    { key: "username", label: "Operator", type: "text",
      description: "An operator created with python -m qsdash.cli create-operator." },
  ],

  setup(ctx) {
    ctx.registerCommand({
      id: "quantsys-connect",
      label: "Connect to quantsys",
      keywords: ["quantsys", "connect", "login", "qsdash"],
      shortcut: "QSC",
      category: "data",
      description: "Log in to qsdash with the operator password and a TOTP code",
      wizardLayout: "form",
      wizard: [
        { key: "password", label: "Password", type: "password", required: true },
        { key: "totp", label: "TOTP code", type: "text", placeholder: "123456", required: true },
      ],
      async execute(values) {
        const baseUrl = ctx.configState.get<string>("baseUrl") ?? "http://127.0.0.1:8000";
        const username = ctx.configState.get<string>("username");
        if (!username) {
          ctx.notify({ body: "Set the operator name first: Set up quantsys.", type: "error" });
          return;
        }
        try {
          await qsClient.login(baseUrl, username, values?.password ?? "", values?.totp ?? "");
          ctx.notify({ body: `Connected to ${new URL(qsClient.getState().baseUrl!).host}.`, type: "success" });
        } catch (error) {
          const message = error instanceof Error ? error.message : String(error);
          ctx.log.error(`quantsys login failed: ${message}`);
          ctx.notify({ body: `quantsys login failed: ${message}`, type: "error" });
        }
      },
    });

    ctx.registerCommand({
      id: "quantsys-disconnect",
      label: "Disconnect from quantsys",
      keywords: ["quantsys", "disconnect", "logout"],
      category: "data",
      hidden: () => !qsClient.getState().connected,
      async execute() {
        qsClient.disconnect();
        ctx.notify({ body: "Disconnected from quantsys.", type: "info" });
      },
    });
  },

  dispose() {
    qsClient.disconnect();
  },

  panes: PANES.map((p): PaneDef => ({
    id: p.id,
    name: p.name,
    icon: p.icon,
    component: p.component,
    defaultPosition: "right",
    defaultMode: "floating",
    defaultFloatingSize: p.size,
  })),

  paneTemplates: PANES.map((p): PaneTemplateDef => ({
    id: `${p.id}-pane`,
    paneId: p.id,
    label: p.name,
    description: p.description,
    keywords: p.keywords,
    shortcut: { prefix: p.shortcut },
    createInstance: () => ({ placement: "floating" }),
  })),
};

export default quantsysPlugin;
