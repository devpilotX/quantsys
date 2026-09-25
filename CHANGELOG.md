# Changelog

Notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow
[semantic versioning](https://semver.org/).

Entries that change a reported performance number are marked **[numbers]**,
because in this project that is the most consequential kind of change.

## [Unreleased]

### Fixed

- **[numbers]** Market impact is now charged on fills, not only in the cost
  gate. `CostModel.order_cost` returns `impact=0` unless a `sigma_daily` is
  supplied, and neither `SimBroker.execute` nor `PaperBroker._fill_one` supplied
  one — so the square-root impact term the cost model documents as "what makes
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
  for every candidate pair forever, reporting "no cointegrated pair found" —
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
  time — including Windows Smart App Control blocking specific binary wheels
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
  change fails the build instead of surfacing in production — which is exactly
  how the ADF defect above was found.
- `SimBroker`'s docstring no longer claims impact is charged, and now states what
  the fill model does *not* do: no size cap against bar volume or ADV, no spread
  cross beyond the flat slippage bps, and gap bars filled at close like any
  other.

## [0.1.0] — 2026-07-02

Initial state inherited from the `Quant12` repository: decision engine,
event-driven backtester, execution layer, dashboard control plane and research
kit, deployed in paper mode. The alpha search was closed with a documented
no-edge verdict (`docs/RESEARCH_CLOSEOUT.md`); Forward Study 2 was running as
the one sanctioned forward-only continuation.
