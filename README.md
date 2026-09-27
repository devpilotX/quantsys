<div align="center">

# quantsys

**Systematic trading for NSE equities and index futures**

A deterministic decision engine, an operator dashboard and a Bloomberg-style terminal.

[![CI](https://github.com/devpilotX/qi/actions/workflows/ci.yml/badge.svg)](https://github.com/devpilotX/qi/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.11%20to%203.14-3776ab)
![Broker](https://img.shields.io/badge/broker-Angel%20One%20SmartAPI-1f6feb)
![Licence](https://img.shields.io/badge/licence-proprietary-555555)

<br>

<img src="docs/images/terminal-monitor.svg" alt="The quantsys Monitor pane in the terminal" width="100%">

<br>

One method, `DecisionEngine.decide()`, runs the backtester, the paper engine and the live engine.<br>
A result from one path holds for the others.

<br>

## How a decision is made

<img src="docs/images/pipeline.svg" alt="The decision pipeline: risk, tier, regime, signals, Kelly, sizing, orders" width="100%">

Every veto and every scaling lands in the decision's audit trail.<br>
[Architecture](docs/ARCHITECTURE.md) has each stage and the reasoning behind it.

<br>

## The terminal

A read-only monitor inside [Gloomberb](https://github.com/gloom-sh/gloomberb), opened by mnemonic from the command bar.

<table>
  <tr>
    <td align="center" width="50%">
      <img src="docs/images/terminal-decision.svg" alt="A decision bar with its full audit trail" width="100%"><br>
      <sub><b>QSD</b> &nbsp; every decision, from regime to audit trail</sub>
    </td>
    <td align="center" width="50%">
      <img src="docs/images/terminal-performance.svg" alt="Equity curve and performance statistics" width="100%"><br>
      <sub><b>QSE</b> &nbsp; equity, drawdown, Sharpe and attribution</sub>
    </td>
  </tr>
  <tr>
    <td align="center" width="50%">
      <img src="docs/images/terminal-positions.svg" alt="Open positions with P&amp;L" width="100%"><br>
      <sub><b>QSP</b> &nbsp; positions, and why each one entered and exited</sub>
    </td>
    <td align="center" width="50%">
      <img src="docs/images/terminal-sleeves.svg" alt="Strategy sleeves with Kelly fractions" width="100%"><br>
      <sub><b>QSS</b> &nbsp; each sleeve's Kelly fraction, edge and P&amp;L</sub>
    </td>
  </tr>
</table>

<sub>Also <b>QS</b> monitor &nbsp;·&nbsp; <b>QSO</b> orders and fills &nbsp;·&nbsp; <b>QSR</b> risk and caps &nbsp;·&nbsp; <b>QSB</b> backtests against the live gate &nbsp;·&nbsp; <b>QSC</b> connect.<br>
Rendered by Gloomberb from demo data.</sub>

<br>

## Strategy sleeves

| Sleeve | Rule | |
|:--|:--|:--:|
| trend | EMA 20/80 spread with a 55-bar breakout | on |
| meanrev | Same-sector cointegrated pairs, enter at \|z\| 2 | on |
| factor | 12-1 momentum and low volatility, long-short, monthly | on |
| expiry | Fade deviations in the last seven days of the month | on |
| downshock | Short after a 3.5-sigma down day on double volume | on |
| tom | Long index futures across the turn of the month | on |
| reversal | Short-term cross-sectional reversal | off |
| voloptions | Option verticals on implied against realized volatility | off |

48 NSE large caps, NIFTY and BANKNIFTY futures, on 15-minute bars. Every parameter lives in [config/base.yaml](config/base.yaml).

<br>

## Risk

| 0.6% | 13% | 18% | 20% | 2.5% |
|:--:|:--:|:--:|:--:|:--:|
| risk per trade | volatility target | drawdown throttle to zero | hard kill, manual re-arm | daily loss stop |

Exposure caps per instrument, sector, correlated cluster, net and margin are re-checked on the final lot-rounded book.<br>
A mismatch with the broker's book freezes all orders.

Live trading stays locked until a real-data backtest clears **OOS Sharpe 0.8, deflated Sharpe 0.95 and P(SR&lt;0) 0.10**,<br>
the broker is connected, `QS_LIVE_ARMED` is set outside the UI, and the operator re-authenticates.

<br>

## Status

Paper only. The 2016 to 2026 research found no rule that beats the index after costs ([closeout](docs/RESEARCH_CLOSEOUT.md), [unseen-data check](reports/unseen_check_2026-09-27.md)).<br>
A pre-registered forward study has run on paper since 2026-07-02 ([registration](docs/FORWARD_STUDY_2.md)).

<br>

## Built with

<sub>Python &nbsp;·&nbsp; NumPy &nbsp;·&nbsp; pandas &nbsp;·&nbsp; SciPy &nbsp;·&nbsp; statsmodels &nbsp;·&nbsp; FastAPI &nbsp;·&nbsp; SQLAlchemy &nbsp;·&nbsp; PostgreSQL &nbsp;·&nbsp; TimescaleDB &nbsp;·&nbsp; Redis &nbsp;·&nbsp; Next.js &nbsp;·&nbsp; React &nbsp;·&nbsp; TypeScript &nbsp;·&nbsp; Bun &nbsp;·&nbsp; Docker</sub>

<br>

## Documentation

[Architecture](docs/ARCHITECTURE.md) &nbsp;·&nbsp; [Development](docs/DEVELOPMENT.md) &nbsp;·&nbsp; [Deployment](docs/DEPLOY.md) &nbsp;·&nbsp; [Go-live](docs/GOLIVE.md) &nbsp;·&nbsp; [Operations](docs/OPS_RUNBOOK.md) &nbsp;·&nbsp; [Environment](docs/ENVIRONMENT.md) &nbsp;·&nbsp; [Security](SECURITY.md) &nbsp;·&nbsp; [Changelog](CHANGELOG.md)

<br>

<sub>Copyright © 2026 devpilotX. All rights reserved. Published for viewing only; see [LICENSE](LICENSE). Not investment advice.</sub>

</div>
