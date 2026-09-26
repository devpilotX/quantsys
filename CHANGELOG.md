# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/).

Entries that change a reported performance number are marked **[numbers]**,
because in this project that is the most consequential kind of change.

## [Unreleased]

An engine review (2026-09) found defects across the backtester, the risk
stack, the signals, the live order path and the paper runner. Each fix below
has a regression test that failed before it. What they mean for the running
forward study is appended to `docs/FORWARD_STUDY_2.md`.

### Fixed: paper and live runner

- **[numbers]** The engine's state now survives a restart. It is saved to a
  new `engine_state` table after every decision and every operator command,
  and restored after warm-up and seeding. Before, the 08:50 recycle cleared the kill latches, measured
  drawdown from that morning, cut every down-shock hold to one session and
  made the monthly factor rebalance run daily. Sessions missed while the
  engine was down still count toward holds and the rebalance clock. A capital
  setting changed while it was down is re-based, not booked as P&L. Needs the
  migration (`qsdash.cli init-db`).
- **[numbers]** Warm-up is sized in sessions of the configured clock and
  covers the regime model's training window. On 15-minute bars it loaded
  about 900 bars, so the live regime HMM never fitted and the pairs sleeve
  never had its lookback. Only the newest 2,000 bars are decided, and replayed
  decisions no longer leave a drawdown reference or a kill latch behind.
- A candle still forming (intraday or today's daily) is not seeded as final.
- A held symbol without a price halts the bar instead of being valued at
  zero; a holding outside the universe raises an alert instead.
- **[numbers]** A paper fill needs a bar for its symbol in the decided bucket,
  as in the backtest.

### Fixed: risk and sizing

- **[numbers]** The drawdown throttle is applied after vol targeting. Before,
  the vol targeter scaled a throttled book back up, so a drawdown often did
  not shrink the book at all.
- **[numbers]** Every exposure cap is re-checked on the final lot-rounded
  targets. Dropping one group could leave an offsetting group above a cap.
- **[numbers]** Min-lot promotion takes the signal's side, scales its risk
  ceiling with the throttle and regime scaler, and never takes a lot past a
  cap. It could turn a zero long into a short lot and take NIFTY-FUT to 54% of
  equity against a 25% cap.
- The anti-churn band no longer holds back a reduction back under a cap or a
  throttle that fell this bar; the dust floor still applies. Hedge legs are
  diffed as a group, all legs or none.
- Kill-switch exits pay market impact; halted bars no longer re-book the last
  unit book into the edge statistics; unreadable equity halts the bar instead
  of disabling both kills; re-arming re-bases the drawdown reference;
  `clear_halt` clears only the reconciliation freeze; a stop cooldown of N
  bars blocks N bars, not N-1; a zero stop is refused, not floored to five
  ticks; disabling a sleeve by config override no longer wedges `post_bar`.
- The Kelly incubation floor is withdrawn only on clearly negative evidence
  (t <= -2), as documented, not on the first negative bar.
- Live drops, whole, any group that would net short a cash equity, so a pair
  never goes out one-legged.

### Fixed: costs

- **[numbers]** Angel One brokerage follows its published schedule (checked
  2026-09-26): equity delivery and intraday min(Rs 20, 0.1%) with a Rs 5
  minimum, F&O Rs 20 per order. Delivery had been modelled as free, which it
  has not been since 2024-11-01. DP charges on delivery sells are still not
  modelled.

### Fixed: signals and regime

- **[numbers]** The down-shock sleeve now matches the frozen research rule it
  is registered as: ddof=1 sigma, de-clustering in sessions from the last
  qualifying shock, every qualifying event recorded, and no event without a
  finite volume history. A test replays one panel through both and requires
  the same events.
- **[numbers]** Factor and reversal rank only names in the tier's view, and a
  rebalance refused for breadth no longer restarts the clock.
- The regime HMM refuses a non-finite fit, the detector drops non-finite
  feature rows, and a refit keeps the calm_trend/calm_range labels of the
  model it replaces.
- The pairs sleeve keeps its re-arm latch across a rescan; `top_k=0` selects
  nothing; the options sleeve reads the bar interval from the bars.

### Fixed: backtest and statistics

- **[numbers]** A walk-forward fold start no longer values a held position
  without a bar at zero, and no order fills at a close from before the
  decision bar.
- **[numbers]** One trade record per round trip. Partial closes were separate
  zero-fee records: on the synthetic fixture, from the same 2,450 fills, 1,263
  records instead of 141 round trips, and a hit rate of 0.52 instead of 0.33.
- **[numbers]** The block bootstrap is circular, so the last OOS return is
  drawn like any other; `P(SR<0)` feeds the go-live gate.
- **[numbers]** Metrics count day one against starting equity and compute
  CAGR over the number of returns; the deflated Sharpe uses the number of
  returns; combined-OOS fees and turnover come from the broker; the pooled
  train-window reference no longer stitches levels from separate runs.
- **[numbers]** PBO averages tied ranks and counts the median rank as one
  half. Pure noise at three configurations scored (n + 1) / (2n) = 0.67 in
  expectation (0.69 over 20 seeds; now 0.51), against a 0.50 gate, and six
  identical configurations scored 1.0. The closed research verdict does not
  change: its deflated Sharpe fails on its own.
- The cross-sectional backtester lets weights drift between rebalances and
  pays for the move from the drifted book, keeps the held book earning
  through a skipped rebalance, stops at `end`, and reports realized beta.
- The verdict's cost check was vacuous (`cost_drag_bps < sharpe * 1e9`); it
  now requires net CAGR > 0. The sweep counts variants that fail to
  configure as trials and lets errors from the run itself propagate.
- A CSV replay no longer stalls a symbol for good after one duplicate
  timestamp.

### Fixed: live order path and dashboard

- Reconciliation compares the engine's own book with one broker read per bar;
  `rebaseline_live_book` clears a freeze once it is explained.
- Write-ahead order journal, lookup by ordertag before any resend, ids with
  the year and a decision/flatten tag, cancel-and-confirm of a resting order
  before a new one on its symbol, and kill exits sized from a fresh book read.
- The postback webhook fails closed without a secret and books the
  incremental price of a partial fill; `livegate` and `preflight` refuse a
  weak secret.
- SmartAPI calls carry explicit timeouts and the SDK can no longer log
  credentials; futures orders go out as CARRYFORWARD under their dated
  tradingsymbol; LIMIT prices are required, snapped to the tick, never more
  aggressive; failed position and funds reads raise instead of reading as a
  flat book.
- The SQL console runs every query in a read-only transaction (with a
  statement timeout on Postgres), refuses the credential tables, and can use a
  dedicated read-only role.
- A refused go-live request no longer leaves its deployable cap in force for
  the paper engine.
- The feed's first token refresh is no longer skipped on a host up for less
  than five minutes (this also made two feed tests fail on fresh CI runners).

### Added

- CI jobs: frontend lint, type check and build; Alembic migrations checked
  against the models.
- Migration `d2e3f4a50002` (`engine_state`).
- Settings `ANGEL_WEBHOOK_ALLOW_UNSIGNED` (dev only) and
  `CONSOLE_DATABASE_URL`; operator command `rebaseline_live_book`.

### Removed

- `ALGO_ASSESSMENT.md`, a June 2026 review whose figures predate the fixes
  above, and two stray editor instruction files in `dashboard/frontend/`.

## [Unreleased: earlier]

### Fixed

- **[numbers]** Market impact is now charged on fills, not only in the cost
  gate. `CostModel.order_cost` returns `impact=0` unless a `sigma_daily` is
  supplied, and neither `SimBroker.execute` nor `PaperBroker._fill_one` supplied
  one, so the square-root impact term the cost model documents as "what makes
  the ADV constraint bind economically at T5/T6" never reached any P&L. The
  engine now publishes its own estimate on `Decision.sigma_daily` and both fill
  paths read it, making the gate and the fill consistent by construction. Every
  backtest and paper figure in `docs/` predating this change understates costs;
  the correction can only make the existing no-edge verdict more negative. The
  dashboard's per-fill `impact` fee line was structurally always zero and is now
  real.
- Two silent-failure paths closed in the numeric layer. `ewma_vol_series` seeded
  its variance with `float(np.nanvar(r)) or 1e-12`, and because NaN is truthy in
  Python an all-NaN return window produced an entirely NaN vol series, which
  `np.clip` does not remove and which therefore entered the HMM feature vector
  as `log(NaN)` with no error. The same idiom was the denominator (`sigma_eq`) of
  the z-score the pairs sleeve trades on.
- The Engle-Granger ADF gate no longer sits inside `except Exception: return
  None`. statsmodels 0.16 changes `adfuller`'s return contract from a tuple to a
  result object; under the old code that would have made `_fit_pair` return None
  for every candidate pair forever, reporting "no cointegrated pair found",
  indistinguishable from the honest answer. The contract is now pinned
  explicitly, both shapes are read correctly, numerical failures are logged, and
  programming errors propagate.
- Real annotation defects surfaced by enabling mypy: `aligned_close_matrix` typed
  its history values as `object`, so a rename of `BarHistory.close` would have
  type-checked and failed at runtime; `compute_metrics` declared an invariant
  `list[tuple[object, float]]` that none of its six real callers could satisfy;
  the Angel front-month resolver could pass `None` as a dict key and rebind a
  non-optional name to `Instrument | None`; `diff_orders` had a `frozenset`
  default that did not satisfy its own `set[str]` annotation.

### Added

- GitHub Actions CI: lint and type gate, a 3.11–3.14 test matrix, the dashboard
  backend suite, a wheel build that installs into a clean virtualenv and asserts
  `py.typed` ships, dependency audit, and full-history secret scanning. A
  determinism pass re-runs the suite under a different `PYTHONHASHSEED` so any
  result depending on hash ordering fails loudly.
- Pre-commit hooks with `gitleaks` ordered first, private-key detection, and
  `scripts/check_no_live_arming.py`, which refuses any commit that sets
  `QS_LIVE_ARMED` truthy in a config file, template, compose file or systemd
  unit. Every other lock in the go-live chain sits downstream of that flag.
- Governance: `LICENSE` (MIT, with an explicit not-investment-advice notice
  pointing at the project's own no-edge verdict), `SECURITY.md` documenting the
  2026-06-11 credential exposure and the known live-path limitations,
  `CONTRIBUTING.md`, `CODEOWNERS`, a pull-request template built around the
  failure modes that have actually occurred here, and Dependabot with the broker
  pins deliberately excluded.
- `docs/ENVIRONMENT.md`: version policy, the verified dependency matrix, the
  determinism guarantees, and the platform failures that have actually cost
  time, including Windows Smart App Control blocking specific binary wheels
  (scipy 1.18.1, sqlalchemy 2.1.x) in a way that takes down unrelated imports.
- `py.typed` (PEP 561): the package now ships its inline annotations.
- 26 tests: 17 covering the NaN and library-contract traps above, 9 covering
  market impact end to end.

### Changed

- Core dependencies gained upper bounds (`numpy<3`, `pandas<4`, `scipy<2`,
  `statsmodels<1`, `pydantic<3`, `PyYAML<7`), recording a range verified against
  Python 3.14.5 / numpy 2.5.3 / pandas 3.0.6 / scipy 1.17.0 / statsmodels 0.15.0
  rather than a guess. The broker extra stays pinned to exact patches.
- Lint ruleset expanded to `I, B, C4, UP, SIM, RUF, NPY, PIE, PGH` with 112
  behaviour-preserving autofixes applied. `DeprecationWarning` and
  `FutureWarning` inside `quantsys.*` are now errors, so a library contract
  change fails the build instead of surfacing in production, which is exactly
  how the ADF defect above was found.
- `SimBroker`'s docstring no longer claims impact is charged, and now states what
  the fill model does *not* do: no size cap against bar volume or ADV, no spread
  cross beyond the flat slippage bps, and gap bars filled at close like any
  other.

## [0.1.0] - 2026-07-02

Initial state inherited from the `Quant12` repository: decision engine,
event-driven backtester, execution layer, dashboard control plane and research
kit, deployed in paper mode. The alpha search was closed with a documented
no-edge verdict (`docs/RESEARCH_CLOSEOUT.md`); Forward Study 2 was running as
the one sanctioned forward-only continuation.
