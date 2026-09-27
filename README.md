# quantsys

quantsys is a systematic trading engine for NSE cash equities and index
futures, trading through Angel One SmartAPI. One method,
`DecisionEngine.decide()`, serves the backtester, the paper runner and the live
runner, so a result from one path is a result from the others.

The dashboard is a FastAPI backend and a Next.js 16 frontend on
Postgres/TimescaleDB and Redis. Login takes an argon2id password and a TOTP
code, every mutation carries a CSRF token, and dangerous actions ask for the
password and code again. It runs with docker compose on a VPS, with systemd
timers for the 08:50 IST engine recycle, a daily self-check and a Saturday
backtest refresh.

## Status

- The 2017 to 2026 alpha search is closed. Nothing cleared the deployment gate;
  [docs/RESEARCH_CLOSEOUT.md](docs/RESEARCH_CLOSEOUT.md) has the evidence.
- Forward Study 2, a pre-registered paper study, started on 2026-07-02. Its first
  read is on 2027-01-05 ([docs/FORWARD_STUDY_2.md](docs/FORWARD_STUDY_2.md)).
- The engine review of September 2026 found defects that change what the paper
  engine does. The fixes are on the `engine-integrity` branch and every one is
  listed in [CHANGELOG.md](CHANGELOG.md). Whether to deploy them into the running
  study is the owner's decision; the "Implementation review" section of
  `docs/FORWARD_STUDY_2.md` sets out what each option costs.
- Live trading is off (`QS_LIVE_ARMED=0`). The Angel One credentials exposed on
  2026-06-11 must be rotated before any live use ([SECURITY.md](SECURITY.md)).

## How a decision is made

Each 15-minute bar, the engine runs one pass over the full universe. The risk
pre-pass applies kill switches and the drawdown throttle. The capital tier sets
how many instruments and sleeves are allowed. A Gaussian HMM estimates the
regime, each enabled sleeve proposes signals, fractional Kelly allocates between
sleeves, and the sizing pipeline turns that into whole lots that pass every
exposure cap and the cost gate. The order diff then decides what actually needs
to trade. Every veto and every scaling is written to the decision's audit trail,
which the dashboard shows per trade.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) has the pipeline in detail, the
reasoning behind each design choice, and the control plane around the engine.

## Configuration

Everything tunable is in [config/base.yaml](config/base.yaml), validated by the
schema in [src/quantsys/config/schema.py](src/quantsys/config/schema.py).

The universe is 48 NSE large caps, NIFTY-FUT, BANKNIFTY-FUT and the NIFTY index,
on 15-minute bars. Capital tiers start at T1 ₹1L, T2 ₹5L, T3 ₹15L, T4 ₹1cr,
T5 ₹5cr and T6 ₹15cr; moving between them changes the instrument and sleeve
limits, the cost gate and the leverage cap, with 10% hysteresis at each boundary.

Six sleeves are on: trend, meanrev (cointegrated pairs), factor, expiry,
downshock and tom (turn of month). Two are off: reversal, whose whole parameter
grid lost money on 2017 to 2026 data, and voloptions, which needs a live option
chain feed that does not exist yet.

Each trade risks 0.6% of equity. The risk budget is throttled linearly as
drawdown approaches 18%, a 20% drawdown is a hard kill that needs a manual
re-arm, and a daily loss of 2.5% stops trading until the next session.

## Live gate

The engine refuses to trade live unless all of these hold
([dashboard/backend/qsdash/bridge/livegate.py](dashboard/backend/qsdash/bridge/livegate.py)):

- the broker adapter is attached and connected;
- `QS_LIVE_ARMED=1` is set in the engine's environment, which the UI cannot do;
- a backtest on real data (not synthetic) has an out-of-sample Sharpe of at
  least 0.8, a deflated Sharpe of at least 0.95 and `P(SR<0)` of at most 0.10;
- `ANGEL_WEBHOOK_SECRET` is at least 32 characters, so fill postbacks cannot be
  forged.

The dashboard adds its own checks in front: a fresh password and TOTP, a typed
confirmation phrase, a cap on deployable capital, and a flat paper book.

## Setup

The engine needs Python 3.11 or newer. The dashboard backend and the Docker
image use 3.12. Keep the virtualenv outside any synced folder.

```bash
python3.12 -m venv ~/.venvs/quant
source ~/.venvs/quant/bin/activate
pip install -e ".[dev,broker]"
pip install -e "dashboard/backend[dev]"
```

On Windows, activate with `~\.venvs\quant\Scripts\Activate.ps1`.

The frontend needs Node 22:

```bash
cd dashboard/frontend && npm ci
```

Credentials go in `.env` at the repository root, which git ignores. Copy
`.env.example` and fill it in; never put real values in the example or in YAML.

## Tests

```bash
python -m pytest tests -m "not network"
cd dashboard/backend && python -m pytest tests
```

The dashboard suite uses SQLite by default. To run it against Postgres, point
`QSDASH_TEST_DATABASE_URL` at a scratch database; its tables are dropped and
rebuilt:

```bash
docker run -d --name qs-pg -p 5432:5432 -e POSTGRES_USER=quantsys \
  -e POSTGRES_PASSWORD=quantsys -e POSTGRES_DB=quantsys \
  timescale/timescaledb:latest-pg16
QSDASH_TEST_DATABASE_URL=postgresql+psycopg://quantsys:quantsys@localhost:5432/quantsys \
  python -m pytest tests
```

CI runs the engine suite on Python 3.11 to 3.14 with pinned dependencies and
repeats it under a second hash seed. It also runs ruff and mypy, the dashboard
suite on SQLite and on TimescaleDB after a real migration, a schema diff between
the migrations and the models, frontend lint, type check and build, both Docker
images through a first deploy, actionlint and shellcheck, a dependency audit, a
full-history gitleaks scan, and a wheel install into a clean virtualenv.

## Backtesting

A synthetic run checks the machinery (accounting, costs, no look-ahead,
determinism), not edge. It takes about 11 minutes:

```bash
python -m quantsys.backtest.runstudy --synthetic --bars 3000 --quick --folds 3
```

For a real study, fetch 15-minute history from Angel One (this needs the
credentials in `.env`), then run the walk-forward on it. `--persist` writes the
result to the dashboard's `backtest_runs` table, which is what the live gate
reads.

```bash
python -m quantsys.data.fetch_history --interval FIFTEEN_MINUTE --out data/nse15
python -m quantsys.backtest.runstudy --replay data/nse15 --persist
```

The research kit in `src/quantsys/research/` runs without the engine or a
broker; install it with `pip install -e ".[research]"`. It builds
survivorship-free daily panels from NSE's public bhavcopy archive and includes
the cross-sectional backtester, PBO and purged K-fold validation used in the
alpha search.

## Running the dashboard

The dashboard commands need Postgres; the default URL matches the container
above. Prepare the database and create an operator (the command prints the TOTP
secret to scan):

```bash
python -m qsdash.cli init-db
python -m qsdash.cli seed-defaults
python -m qsdash.cli create-operator --username NAME
python -m qsdash.cli reset-password --username NAME
```

`init-db` applies the migrations and seeds the defaults, so `seed-defaults` is
only needed on a database that was migrated some other way. `create-operator`
and `reset-password` prompt for the password.

Start the API and the frontend, then open http://localhost:3000:

```bash
uvicorn qsdash.main:app --port 8000
cd dashboard/frontend && npm run dev
```

To see the dashboard move without a broker, replay synthetic bars through the
paper engine. 4,000 bars take about 16 minutes:

```bash
python -m qsdash.bridge.runner --synthetic --bars 4000 --speed 0
```

The paper runner on live Angel One data needs the credentials:

```bash
python -m qsdash.bridge.live --paper
```

## Deployment

[docs/DEPLOY.md](docs/DEPLOY.md) covers the VPS: docker compose, nginx and TLS,
the systemd timers, backups and the first `init-db`.
[docs/GOLIVE.md](docs/GOLIVE.md) is the checklist that has to be complete before
live trading, and [docs/OPS_RUNBOOK.md](docs/OPS_RUNBOOK.md) covers day-to-day
operation.

## Cost model

The cost model in `src/quantsys/costs.py` charges every fill in backtest and
paper, and the cost gate uses the same numbers. Checked on 2026-09-26:

- STT on futures is 0.05% of the sell side and on options 0.15% of premium, from
  2026-04-01 ([Moneycontrol](https://www.moneycontrol.com/news/business/markets/up-to-150-stt-hike-comes-into-effect-from-april-1-here-s-how-it-will-impact-f-o-traders-13875419.html)).
- NSE transaction charges are ₹2.97 per lakh for equity, ₹1.73 per lakh for
  futures and ₹35.03 per lakh of option premium, since 2024-10-01
  ([Zerodha bulletin](https://zerodha.com/marketintel/bulletin/391488/revision-in-transactions-charges-from-1st-october-2024)).
  The model adds the IPFT levy on top.
- Angel One brokerage is min(₹20, 0.1%) per equity order with a ₹5 minimum,
  delivery included, which it has charged since 2024-11-01, and ₹20 per F&O
  order ([Angel One](https://www.angelone.in/support/charges-and-cashbacks/brokerage-charges)).
- Lot sizes are 65 for NIFTY and 30 for BANKNIFTY from the January 2026 series
  ([Zerodha bulletin](https://zerodha.com/marketintel/bulletin/429705/revision-in-lot-size-of-index-derivative-contracts-from-december-30-2025)).
- Monthly contracts expire on the last Tuesday of the month, since 2025-09-01
  ([Mint](https://www.livemint.com/market/stock-market-news/nse-f-o-expiry-shifts-to-tuesday-bse-to-thursday-from-september-1-details-here-11750214653248.html)).
- Depository (DP) charges on delivery sells are not modelled.

Lot sizes and ADV in the config are fallbacks; the live runner refreshes them
from the Angel One instrument master.

## Repository layout

```
src/quantsys/        engine: config, data, costs, regime, strategies,
                     portfolio, risk, engine, backtest, execution, options,
                     research
config/base.yaml     the deployed configuration and universe
tests/               engine tests
dashboard/backend/   FastAPI app, engine bridge, Alembic migrations, tests
dashboard/frontend/  Next.js app
deploy/              docker compose, nginx, systemd units, ops scripts
constraints/         pinned dependency sets per Python version
docs/                architecture, decisions, studies, runbooks
```

## Licence

MIT. See [LICENSE](LICENSE), which also states that nothing here is investment
advice.
