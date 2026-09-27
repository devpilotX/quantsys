# Development

Setup, tests and the commands that run each part of the system. The rules
for changing it are in [CONTRIBUTING.md](../CONTRIBUTING.md), version
pinning in [ENVIRONMENT.md](ENVIRONMENT.md), and deployment in
[DEPLOY.md](DEPLOY.md).

## Setup

The engine needs Python 3.11 or newer; the dashboard backend and the Docker
image use 3.12. Keep the virtualenv outside any synced folder.

```bash
python3.12 -m venv ~/.venvs/quant
source ~/.venvs/quant/bin/activate      # Windows: ~\.venvs\quant\Scripts\Activate.ps1
pip install -e ".[dev,broker]"
pip install -e "dashboard/backend[dev]"
pip install -e ".[research]"            # only for quantsys.research
```

The web frontend needs Node 22 (`cd dashboard/frontend && npm ci`); the
terminal plugin needs Bun 1.3 (`cd terminal && bun install`).

Credentials go in `.env` at the repository root, which git ignores. Copy
`.env.example` and fill it in; never put real values in the example or in
YAML.

## Tests

```bash
python -m pytest tests -m "not network"
cd dashboard/backend && python -m pytest tests
```

The dashboard suite uses SQLite by default. Point `QSDASH_TEST_DATABASE_URL`
at a scratch Postgres to run it there; its tables are dropped and rebuilt:

```bash
docker run -d --name qs-pg -p 5432:5432 -e POSTGRES_USER=quantsys \
  -e POSTGRES_PASSWORD=quantsys -e POSTGRES_DB=quantsys \
  timescale/timescaledb:latest-pg16
QSDASH_TEST_DATABASE_URL=postgresql+psycopg://quantsys:quantsys@localhost:5432/quantsys \
  python -m pytest tests
```

The terminal plugin type-checks and tests against a linked Gloomberb
checkout, the way Gloomberb loads plugins; the `terminal` job in
`.github/workflows/ci.yml` pins the revision.

```bash
cd terminal
ln -s /path/to/gloomberb node_modules/gloomberb
ln -s /path/to/gloomberb/node_modules/react node_modules/react
bun run typecheck && bun test
```

`terminal/e2e.test.ts` reads every endpoint the panes use from a running
qsdash when `QS_E2E_URL`, `QS_E2E_USER`, `QS_E2E_PASSWORD` and `QS_E2E_TOTP`
are set, and is skipped otherwise.

## Backtesting

A synthetic run checks the machinery (accounting, costs, no look-ahead,
determinism), not edge. It takes about 11 minutes:

```bash
python -m quantsys.backtest.runstudy --synthetic --bars 3000 --quick --folds 3
```

A real study replays 15-minute history fetched from Angel One (needs the
credentials). `--persist` writes the result to `backtest_runs`, which the
live gate reads.

```bash
python -m quantsys.data.fetch_history --interval FIFTEEN_MINUTE --out data/nse15
python -m quantsys.backtest.runstudy --replay data/nse15 --persist
```

The research kit rebuilds the daily NSE panels from the public bhavcopy
archive into `data_cache/` and re-runs the closed studies:

```bash
python -m quantsys.research.run_combine
python scripts/_study2_sanity_checks.py
```

## Dashboard

The commands below need Postgres; the default `DATABASE_URL` matches the
container above.

```bash
python -m qsdash.cli init-db                       # migrations, defaults, console role
python -m qsdash.cli create-operator --username NAME
python -m qsdash.cli reset-password --username NAME
python -m qsdash.cli console-role                  # only when CONSOLE_DATABASE_URL changes
uvicorn qsdash.main:app --port 8000
cd dashboard/frontend && npm run dev
```

`create-operator` prints the TOTP secret to scan. To see the dashboard move
without a broker, replay synthetic bars through the paper engine (4,000 bars
take about 16 minutes):

```bash
python -m qsdash.bridge.runner --synthetic --bars 4000 --speed 0
```

The paper runner on live Angel One data needs the credentials:

```bash
python -m qsdash.bridge.live --paper
```

## Terminal

Link the plugin into Gloomberb, set the qsdash URL and operator with
`Set up quantsys`, then connect with `QSC` (password and TOTP code):

```bash
gloomberb plugin link ./terminal
gloomberb plugin doctor gloom-quantsys
```
