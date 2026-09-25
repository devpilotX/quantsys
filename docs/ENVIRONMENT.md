# Environment and reproducibility

Every performance number this project has ever reported was produced by a
specific numerical stack. Recording which one is part of the result, not
housekeeping.

## Version policy

`pyproject.toml` declares a **compatible range** with upper bounds. Exact pins
belong in a lockfile or constraints file per deployment, not in library
metadata.

Upper bounds exist because the stack is load-bearing: numpy 2 changed dtype
promotion, pandas 3 changed copy-on-write semantics, and statsmodels is
mid-migration on `adfuller`'s return contract. A silent major bump changes
arithmetic underneath the cost and sizing math. The broker extra
(`smartapi-python`, `websocket-client`, `pyotp`, `logzero`) is pinned to exact
patch versions for a different reason, documented inline in `pyproject.toml`:
those four have a callback-signature coupling that breaks in both directions.

## Verified configurations

| Python | numpy | pandas | scipy | statsmodels | pydantic | Result |
|---|---|---|---|---|---|---|
| 3.14.5 | 2.5.3 | 3.0.6 | 1.17.0 | 0.15.0 | 2.13.5 | 217 engine tests green, 60 dashboard green |

The CI matrix covers 3.11, 3.12, 3.13 and 3.14 on Linux. 3.11 is the declared
floor; 3.14 is what the maintainer runs locally. Both ends are tested because
the numerical stack does not behave identically across them.

Note the mismatch between the package floor (3.11) and mypy's `python_version`
(3.12): numpy 2.5's bundled stubs use PEP 695 `type` statements, which mypy
refuses to parse under 3.11 and which aborts the run before it reaches any
project code. Runtime 3.11 support is therefore proven by the test matrix rather
than by the type checker.

## Determinism

Research results must not depend on anything incidental:

- `PYTHONHASHSEED=0` in CI, and a second test pass under a different hash seed,
  so a result that depends on dict or set iteration order fails loudly.
- RNGs are seeded explicitly everywhere they affect a number: synthetic bars
  (seed 7), Monte-Carlo block bootstrap (seed 11), PBO/CSCV (seed 7), HMM
  restarts.
- No `datetime.now()` in any numeric path. The engine's timestamps are
  IST-naive by contract and normalised at the data layer, so backtest and live
  traverse identical code.
- The engine's persisted state round-trips to bit-identical subsequent
  decisions, which is asserted by the test suite.

## Platform problems that have actually occurred

### Windows Smart App Control blocks binary wheels

**Symptom.** An import fails with:

```
ImportError: DLL load failed while importing _distance_pybind:
An Application Control policy has blocked this file.
```

One blocked `.pyd` takes down far more than itself: a blocked
`scipy/spatial/_distance_pybind` made `scipy.stats`, `scipy.signal`,
`scipy.optimize` and all of `statsmodels` unimportable, which is 9 of 14 test
modules.

**Cause.** Smart App Control (`HKLM\SYSTEM\CurrentControlSet\Control\CI\Policy`
→ `VerifiedAndReputablePolicyState = 1`) blocks binaries it does not consider
reputable. Reputation is per-file, so this is version-specific, not
package-specific: numpy and pandas loaded fine while scipy 1.18.1 did not.

**Fix.** Install a version whose binary is already trusted. Observed on one
machine in September 2026:

| Package | Blocked | Works |
|---|---|---|
| scipy | 1.18.1 | 1.17.0, 1.16.2 |
| sqlalchemy | 2.1.x | 2.0.44, 2.0.36 |

`Unblock-File` does not help — that clears the zone marker, which is a different
mechanism.

**Do not disable Smart App Control to work around this.** Turning it off cannot
be undone without reinstalling Windows, and it is a real protection. Pin a
working version instead, or develop in WSL2 or a container where the policy does
not apply.

### Virtualenvs inside synced folders

Keep the venv outside OneDrive/Dropbox. Sync churn corrupts virtualenvs in ways
that present as bizarre import errors. Hence `~/.venvs/quant` rather than a
`.venv` beside the source.

## Reproducing a reported number

1. Check out the commit the number was reported at.
2. Install with the constraints recorded for that commit.
3. Confirm the suite is green **before** running the study. A green suite is
   part of the result's provenance.
4. Note that results reported before the market-impact fix understate costs:
   impact was charged in the cost gate but never on any fill, so historical
   figures in `docs/` are optimistic by the whole impact term. The correction can
   only make the existing no-edge verdict more negative.
