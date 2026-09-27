# quantsys

quantsys is a systematic trading engine for NSE cash equities and index
futures, trading through Angel One SmartAPI. A single method,
`DecisionEngine.decide()`, runs the backtester, the paper engine and the live
engine, so a result from one path holds for the others. Around it sit an
operator dashboard (FastAPI and Next.js on Postgres/TimescaleDB) and a
Bloomberg-style terminal built as a [Gloomberb](https://github.com/gloom-sh/gloomberb)
plugin.

## Status

| | |
|---|---|
| Trading | Paper only. Live is disarmed (`QS_LIVE_ARMED=0`). |
| Research | The 2016 to 2026 alpha search is closed: no rule cleared the deployment gate ([evidence](docs/RESEARCH_CLOSEOUT.md)). |
| Forward study | Forward Study 2, pre-registered, running on paper since 2026-07-02; first read 2027-01-05 ([registration](docs/FORWARD_STUDY_2.md)). |
| Engine review | The September 2026 fixes are on `main` ([CHANGELOG](CHANGELOG.md)). Deploying them into the running study is an open decision ([#26](https://github.com/devpilotX/quant/issues/26)). |
| Before live | The credentials exposed on 2026-06-11 must be rotated ([SECURITY.md](SECURITY.md)), and the order path verified against the live API ([#21](https://github.com/devpilotX/quant/issues/21)). |

## How a decision is made

Every 15-minute bar the engine makes one deterministic pass over the whole
universe:

1. The risk pre-pass applies the kill switches, the daily loss stop and the
   drawdown throttle.
2. Equity sets the capital tier: how many instruments and sleeves may trade,
   the leverage cap and the cost gate, with 10% hysteresis between tiers.
3. A Gaussian HMM estimates the probability of a calm trend, a calm range or
   turbulence, and scales risk and sleeve weights.
4. Each enabled sleeve proposes positions with a stop.
5. Fractional Kelly allocates between sleeves on their measured edge, tilted
   by the regime.
6. Sizing puts a fixed fraction of equity at risk per trade, scales the book
   to a volatility target, cuts it to every exposure cap, rounds to whole lots
   and re-checks every cap and the cost gate. A pair whose hedge ratio breaks
   in rounding is dropped whole.
7. The order diff against the current book applies anti-churn bands and an
   execution style per tier.

Every veto and every scaling is written to the decision's audit trail, and
both front ends show it trade by trade.
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the pipeline in detail and the
reasoning behind each design choice.

## Strategy sleeves

| Sleeve | Rule | State |
|---|---|---|
| trend | Time-series momentum: EMA 20/80 spread and a 55-bar breakout, 2.5 ATR stop | on |
| meanrev | Same-sector cointegrated pairs (Engle-Granger, OU fit); enter at \|z\| 2.0, exit 0.5, stop 3.5 | on |
| factor | Cross-sectional 12-1 momentum and low volatility, 12 long and 12 short, monthly | on |
| expiry | Fade deviations from the rolling mean in the last 7 days of the month (F&O settlement) | on |
| downshock | Short a stock after a 3.5-sigma down day on 2x volume, hold 10 sessions | on |
| tom | Long NIFTY and BANKNIFTY futures from 2 weekdays before month end to 3 after | on |
| reversal | Short-term cross-sectional reversal | off: every parameter cell lost money on 2017 to 2026 data |
| voloptions | Defined-risk option verticals on implied against realized volatility | off: needs a live option-chain feed |

The universe is 48 NSE large caps, NIFTY-FUT, BANKNIFTY-FUT and the NIFTY
index. All parameters are in [config/base.yaml](config/base.yaml), validated
by [src/quantsys/config/schema.py](src/quantsys/config/schema.py).

## Risk controls

| Control | Setting |
|---|---|
| Risk per trade | 0.6% of equity |
| Volatility target | 13% annualized, scaler bounded to 0.25x to 2x |
| Drawdown throttle | Risk scales down linearly to zero at an 18% drawdown |
| Hard kill | 20% drawdown flattens the book and needs a manual re-arm |
| Daily stop | A 2.5% daily loss stops trading until the next session |
| Exposure caps | 25% of equity per instrument, 50% per sector, 150% net, 40% per correlated cluster, 60% margin |
| Capital tiers | T1 ₹1L, T2 ₹5L, T3 ₹15L, T4 ₹1cr, T5 ₹5cr, T6 ₹15cr |
| Reconciliation | A mismatch between the engine's book and the broker's freezes all orders |

The engine refuses to trade live unless every one of these holds
([livegate.py](dashboard/backend/qsdash/bridge/livegate.py)): a connected
broker adapter; `QS_LIVE_ARMED=1` set in the engine's environment, which no UI
can do; a backtest on real data with an out-of-sample Sharpe of at least 0.8, a
deflated Sharpe of at least 0.95 and `P(SR<0)` of at most 0.10; and a postback
secret of at least 32 characters. The dashboard adds a fresh password and TOTP
check, a typed confirmation, a deployable-capital cap and a flat paper book.

## Costs

Every fill in backtest and paper pays the modelled cost, and the cost gate
uses the same numbers. Checked on 2026-09-27:

| Charge | Rate | Source |
|---|---|---|
| STT | Futures 0.05% on sells, options 0.15% of premium, from 2026-04-01; delivery equity 0.1% both sides | [Moneycontrol](https://www.moneycontrol.com/news/business/markets/up-to-150-stt-hike-comes-into-effect-from-april-1-here-s-how-it-will-impact-f-o-traders-13875419.html) |
| NSE transaction charges | ₹2.97 per lakh equity, ₹1.73 futures, ₹35.03 option premium, plus IPFT | [Zerodha bulletin](https://zerodha.com/marketintel/bulletin/391488/revision-in-transactions-charges-from-1st-october-2024) |
| Brokerage | Equity min(₹20, 0.1%) with a ₹5 minimum, delivery included; ₹20 per F&O order | [Angel One](https://www.angelone.in/support/charges-and-cashbacks/brokerage-charges) |
| DP charge | ₹20 + GST per delivery sell | [Angel One](https://www.angelone.in/exchange-transaction-charges) |
| Stamp duty, SEBI fee, GST | Stamp on buys (0.015% delivery), ₹10 per crore, 18% on fees | [Angel One](https://www.angelone.in/exchange-transaction-charges) |
| Slippage and impact | 3 bps equity, 1.5 bps futures, plus square-root impact on participation | model |

Contract specifications follow the exchange: NIFTY lots of 65 and BANKNIFTY
lots of 30 from the January 2026 series
([Zerodha bulletin](https://zerodha.com/marketintel/bulletin/429705/revision-in-lot-size-of-index-derivative-contracts-from-december-30-2025)),
and monthly expiry on the last Tuesday since 2025-09-01
([Mint](https://www.livemint.com/market/stock-market-news/nse-f-o-expiry-shifts-to-tuesday-bse-to-thursday-from-september-1-details-here-11750214653248.html)).
The live runner refreshes lot sizes from the Angel One instrument master.

## Research record

The alpha search tested trend, cointegrated pairs, expiry and volatility
effects, cross-sectional factors, BANKNIFTY put-call flows and a
risk-parity blend of the two survivors, on survivorship-free daily panels built
from NSE's public archive. The blend came closest (out-of-sample Sharpe 0.87)
and still failed the deflated Sharpe and overfitting tests. Re-run unchanged on
the 68 sessions after the closeout, every sleeve trailed the market: the blend
lost 4.4% while an equal-weight top-100 basket gained 6.6%
([report](reports/unseen_check_2026-09-27.md)). The honest conclusion is that
none of these rules beats the index, and the only legitimate test left is
forward, on rules registered before they see data.

## Components

| Component | What it is |
|---|---|
| [src/quantsys](src/quantsys) | The engine: data, regime, strategies, allocation, sizing, risk, costs, order diff, backtester, Angel One adapter and OMS |
| [src/quantsys/research](src/quantsys/research) | Standalone research kit: NSE bhavcopy and F&O panels, cross-sectional backtester, deflated Sharpe, PBO, purged K-fold |
| [dashboard/backend](dashboard/backend) | FastAPI control plane and engine bridge: argon2id and TOTP login, CSRF, re-auth for dangerous actions, audit log, Alembic migrations |
| [dashboard/frontend](dashboard/frontend) | Next.js 16 operator dashboard |
| [terminal](terminal) | Gloomberb plugin: a read-only terminal monitor of the engine |
| [deploy](deploy) | Docker compose, nginx, systemd timers (08:50 IST recycle, daily self-check, Saturday backtest refresh), backups |

The terminal opens from the Gloomberb command bar by mnemonic:

| Mnemonic | Pane |
|---|---|
| `QS` | Monitor: engine state, equity, drawdown, tier, regime, recent risk events |
| `QSP` | Positions, with why each one entered and exited |
| `QSO` | Order journal and fills with the fee breakdown |
| `QSD` | Decisions: signals, Kelly weights, targets, orders and the full audit trail |
| `QSS` | Sleeves: Kelly fraction, edge statistics and booked P&L |
| `QSE` | Performance: equity and drawdown, Sharpe, costs, P&L by regime and symbol |
| `QSR` | Risk: exposure, regime mix, the caps that bound on the last bar |
| `QSB` | Backtests and whether each clears the live gate |

## Documentation

[Architecture](docs/ARCHITECTURE.md) · [Development](docs/DEVELOPMENT.md) ·
[Environment](docs/ENVIRONMENT.md) · [Deployment](docs/DEPLOY.md) ·
[Go-live checklist](docs/GOLIVE.md) · [Operations](docs/OPS_RUNBOOK.md) ·
[Decisions](docs/DECISIONS.md) · [Contributing](CONTRIBUTING.md) ·
[Security](SECURITY.md)

## Licence

MIT. See [LICENSE](LICENSE), which also states that nothing here is
investment advice.
