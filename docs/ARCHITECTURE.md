# Architecture

This document covers the decision engine first, then the control plane that
runs it on the VPS.

## Decision engine

`DecisionEngine.decide()` makes one deterministic pass per decision bar. The
backtester, the paper runner and the live runner build the same `MarketState`
and call the same method, so there is no second implementation to drift.

```
MarketState (bars, equity, positions)
        |
        v
DecisionEngine.decide()
  1  risk pre-pass        kill switches, drawdown throttle          risk/engine.py
  2  capital tier         gates + interpolated params, hysteresis   portfolio/tiers.py
  3  regime               Gaussian HMM -> P(calm_trend/range/turb)  regime/
  4  gating               universe size, strategy count per tier    engine/decision.py
  5  signals              one sleeve per strategy module            strategies/
  6  stop vetoes          hard and trailing stops, cooldowns        risk/engine.py
  7  Kelly allocation     f = clip(k*mu/var, 0, cap) * regime_w     portfolio/allocation.py
  8  sizing pipeline      risk-frac qty -> vol target -> caps       portfolio/, risk/rules.py
                          -> lots -> cap re-check -> cost gate
  9  order diff           anti-churn bands, urgency, exec style     engine/orders.py
        |
        v
Decision (targets, orders, full audit trail)
```

Every veto and every scaling writes an `AuditEvent`, and the dashboard's
explain view renders that trail as recorded.

### Design decisions

1. The decision clock is a config value. The schema default is 5 minutes and
   `config/base.yaml` runs 15. At retail fee levels the cost gate vetoes nearly
   everything signal-able on 1-minute bars.
2. Volatility targeting uses the instrument covariance of the proposed book,
   not strategy-return covariance: it is observable from day one, well
   conditioned, and measures the quantity being capped. Strategy covariance
   still enters through the per-strategy Kelly statistics. A symbol with no
   covariance history means the targeter may only shrink the book.
3. A strategy cannot earn statistics without capital, so a strategy with
   `n_eff < ramp_obs` gets a small incubation floor. It is withdrawn only when
   the t-statistic on the shrunk mean is at or below `-ramp_withdraw_t`.
4. Edge statistics run on virtual unit books (unit sizing, proportional costs
   only). This removes the feedback loop between allocation and measured
   returns and keeps flat per-order fees, which depend on scale, out of a
   scale-free estimate.
5. There are two halt semantics. A kill (market risk, our state trusted)
   flattens everything at market. A reconciliation halt (state not trusted)
   freezes with no orders at all, because flattening on top of a wrong book can
   double the error. The broker is ground truth.
6. A multi-leg trade is one signal with legs. Every risk scaling acts on the
   group jointly, so no cap can orphan a hedge leg; if any leg rounds to zero
   lots, or lot rounding moves the hedge ratio more than
   `sizing.max_hedge_ratio_drift`, the whole group is dropped. The final
   lot-rounded targets are re-checked against every cap.
7. Pair parameters freeze for the length of a trade episode. After a stop-out
   or time stop the pair is latched until the spread revisits
   `|z| < z_entry`, which prevents instant re-entry into a stuck dislocation.
8. The cost gate applies to new positions only. Gating a held position would
   force payment of the exit cost the gate exists to avoid. Equity trades are
   gated at delivery STT, the worst case, and the gate blocks tight-stop equity
   trading at small tiers by design.
9. The HMM is under 200 lines in the repository instead of a dependency:
   deterministic seeding, explicit degeneracy reporting, no exotic packages. A
   degenerate or unconverged fit is refused and the detector falls back to a
   volatility-percentile classifier, and to a neutral warm-up state before
   that.
10. Tier boundaries have 10% hysteresis, so an account oscillating around a
    threshold does not flap its configuration; demotion cascades after a
    crash.
11. Anti-churn: a tier-dependent rebalance band, a minimum order notional
    scaled to equity, and conviction floors on trend holds. Full exits and
    risk-reducing orders always pass the band.
12. Timestamps are IST-naive by contract. The live data layer normalises them
    before the engine sees anything, so backtest and live run identical code.
13. Equity at or below zero is an immediate hard kill, and unreadable equity
    halts the bar rather than being stored.
14. Engine state is persisted after every decision and every operator command
    and restored after warm-up, so the daily 08:50 recycle keeps kill
    latches, drawdown references, holds and rebalance clocks.

## Control plane

```
                       ┌────────────────────────── VPS (quant.devpilotx.com) ─────────────────────────┐
                       │                                                                              │
  Browser (operator)   │   nginx :443 (TLS, HSTS, rate limits)                                        │
  ───────────────────► │   ├── /            → frontend  (Next.js 16, dark dense SPA)                  │
   session cookie      │   ├── /api/…       → api       (FastAPI: auth, REST snapshots, control)      │
   httpOnly+Strict     │   ├── /ws          → api       (websocket hub)                               │
   + CSRF header       │   └── /dbadmin     → pgweb     (IP-gated DB admin)                           │
                       │                                                                              │
                       │   api ──reads──────────► PostgreSQL (TimescaleDB) ◄──────writes── engine     │
                       │    │                       21 tables: decisions, orders, fills,   │          │
                       │    │                       positions, equity_curve, risk_events,  │          │
                       │    │                       regime_history, audit_log, commands…   │          │
                       │    │                                                              │          │
                       │    └─subscribe── Redis pub/sub (or PG LISTEN/NOTIFY) ◄──publish───┘          │
                       │                  same envelope either way                                    │
                       │                                                                              │
                       │   engine = quantsys DecisionEngine + qsdash bridge:                          │
                       │     Recorder      every decide() pass → decisions row (full audit) + events  │
                       │     PaperBroker   fills at bar close with the REAL CostModel fee breakdown   │
                       │     CommandConsumer  polls `commands` between bars; acks/rejects             │
                       │     LiveExecutionBroker  Angel One OMS; live only after the live gate opens  │
                       └──────────────────────────────────────────────────────────────────────────────┘
```

## Trust boundaries & invariants

1. **The engine is the only writer of trading truth.** The API writes only
   auth/audit/config/command rows. The dashboard renders DB rows verbatim:
   `decisions.audit` is the engine's own AuditEvent trail, never re-derived.
2. **Control actions are commands, not writes.** API → `commands` table →
   engine consumes, acts, acks with a result. `runtime_config['mode']` is
   flipped only by the engine, so the mode badge always shows engine truth.
   `mode_requested` shows the pending transition.
3. **Process isolation.** engine, api, frontend, postgres, redis, nginx are
   separate supervised containers; any dashboard component can die without
   touching trading.
4. **Events commit with rows.** With the PG bus, `pg_notify` fires inside the
   recorder's transaction: a client can never see an event for a row that
   rolled back. With Redis, publish happens after commit (at-least-once via
   client snapshot refetch on reconnect).
5. **Reconnect = resync.** The frontend invalidates every query on WS
   (re)connect; deltas are only trusted on top of a fresh snapshot. The
   connection state and engine heartbeat age are always visible; stale is
   rendered as stale.

## Auth model (single operator, real money)

- argon2id password + **mandatory TOTP** (pyotp, ±1 step skew).
- Sessions: 48-byte token, sha256-hashed at rest, httpOnly + Secure +
  SameSite=Strict cookie; sliding idle timeout (60 min) + absolute cap (12 h).
- CSRF: double-submit cookie → `X-CSRF-Token` header on every mutation.
- Lockout: 5 failures → 15 min; identical error for wrong user/pass/TOTP.
- **High-risk actions** (mode, capital, kill, flatten, config, strategy
  toggle) additionally require re-auth (password+TOTP) within a 5-minute
  freshness window: enforced server-side by `require_fresh_reauth`.
- Going LIVE additionally requires the typed phrase `GO LIVE REAL MONEY`,
  an explicit deployable-capital cap, and a flat (or explicitly flattened)
  paper book. All four checks are server-side.
- Every login/control action lands in `audit_log` with IP.

## Real-time pipeline

- Topics: decisions, orders, fills, positions, equity, regime, risk, alerts,
  engine (heartbeat), config, commands.
- Bus backends behind one interface (`qsdash/bus.py`): Redis pub/sub in
  production, Postgres LISTEN/NOTIFY when Redis is absent (Windows dev).
  The PG listener runs on a dedicated thread (sync psycopg) bridged into the
  event loop: immune to ProactorEventLoop limitations.
- WS hub (`qsdash/ws.py`): cookie-authenticated, topic-filtered fan-out,
  20 s ping. Oversized NOTIFY payloads degrade to `{ref:…}` and clients
  refetch: nothing is truncated.

## Mode & capital semantics

- `mode` ∈ {paper, live}; every trading row carries it; the two universes are
  never aggregated.
- Paper: `E(t)` = PaperBroker equity (cash + MTM), seeded from operator-set
  `paper_capital` (₹1L–₹20cr): simulate any tier on demand.
- Live (when the adapter ships): `E(t)` from broker balance, clamped by
  `min(E, E×deployable_cap_frac, deployable_cap_abs)` in
  `Runner.effective_equity`: the algo cannot size beyond the cap.
- Kill semantics mirror the engine core: operator kill arms the
  max-drawdown latch (flatten everything, manual re-arm); reconciliation
  halt freezes with no orders at all (state not trusted).

## Repo layout (dashboard additions)

```
dashboard/backend/qsdash/
  config.py security.py db.py models.py deps.py bus.py audit.py notify.py
  main.py ws.py
  api/ auth control data dbadmin webhooks
  bridge/ recorder paper commands runner
  alembic/ (migrations; conditional Timescale hypertable)
  tests/ scripts/
dashboard/frontend/src/
  lib/ api format live
  components/ ui shell charts audit reauth
  app/ login + (dash)/{overview,positions,pnl,explain,strategies,regime,
                       risk,capital,backtests,logs,db,settings}
deploy/ docker-compose.yml nginx/ scripts/ .env.example
```
