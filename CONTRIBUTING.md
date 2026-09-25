# Contributing

This is a single-maintainer research and trading system. The bar for changes is
set by one question: **would this change make a reported number wrong, or a
placed order wrong?** Everything below follows from that.

## Setup

The virtualenv must live outside any synced folder (OneDrive/Dropbox sync churn
corrupts virtualenvs):

```bash
python -m venv ~/.venvs/quant          # Windows: C:\Users\<you>\.venvs\quant
~/.venvs/quant/bin/python -m pip install -e ".[dev,broker]"
~/.venvs/quant/bin/pre-commit install --hook-type pre-commit --hook-type pre-push
```

Then confirm a clean baseline before changing anything:

```bash
python -m pytest tests -q        # expect all green
python -m ruff check .
python -m mypy
```

Dashboard backend has its own package and suite:

```bash
cd dashboard/backend
python -m pip install -e ".[dev]"
python -m pytest tests -q
```

See `docs/ENVIRONMENT.md` for version pinning policy and for the
platform-specific install problems that have actually bitten (blocked binary
wheels under Windows Smart App Control, among others).

## The rules that matter

**1. Never mix a behaviour change with a formatting change.** A reviewer must
be able to see the whole of a behaviour change in one small diff. Lint and
reformat in their own commits.

**2. A cost charged in the gate must be charged on the fill.** The cost gate
decides whether a trade is worth doing; the fill decides what it cost. If these
disagree, the backtest manufactures edge. This has already happened once
(market impact). Both paths read from `Decision.sigma_daily` now — keep it that
way.

**3. No look-ahead, and prove it.** A decision on bar `t` may use only
information available at `t`. Note that the backtester currently fills at the
same bar's close, which is the most optimistic defensible assumption; do not
make it more optimistic.

**4. Do not re-litigate the alpha verdict on the 2017–2026 sample.** The
research is closed (`docs/RESEARCH_CLOSEOUT.md`): nothing cleared the
deployment gate, and PBO on that sample is 0.84, meaning further tuning
manufactures a false edge rather than finding a real one. New strategy work must
be a **separately pre-registered, forward-only** study on unseen data. A pull
request that improves an in-sample number will be declined on principle.

**5. Failures must be observable.** `except Exception: return None` around a
strategy gate is how a sleeve silently stops working forever while its logs stay
clean. Narrow the exception, log it, and let programming errors propagate.

**6. `float(x) or default` is banned.** NaN is truthy, so the default is not
applied and a NaN propagates into a denominator. Use an explicit finite check.

**7. Every risk cap must shrink, never grow.** The one intentional exception is
the volatility targeter, which can scale a book *up* toward its vol target; it
must therefore run before the exposure caps so the caps bind last.

**8. Multi-leg groups scale jointly.** No cap may orphan one leg of a hedge. If
any leg rounds to zero lots, the whole group is dropped.

**9. Never commit a live-arming flag.** `QS_LIVE_ARMED` is set by hand on the
host, after credential rotation. A pre-commit hook enforces this.

## Tests

- A bug fix needs a regression test that fails before the fix. If you cannot
  write one, say so in the pull request and explain why.
- Test behaviour, not implementation, so refactoring does not break the suite.
- Tests must be order-independent. Module-level mutable state in a test is a
  defect; it has already caused a cascade of unrelated failures here.
- Property-based tests (`hypothesis`) are preferred for the risk and sizing
  invariants, because the interesting inputs are the ones nobody thinks to pick.
- Anything touching the network must be marked `@pytest.mark.network` and must
  not run in CI.

## Commit messages

Conventional-commit prefix, then explain *why* in the body. The existing history
is the style guide: state what was wrong, what the consequence was, and what the
fix does. A message that only restates the diff is not useful six months later.

```
fix(costs): charge market impact on fills, not only in the cost gate
types: make mypy pass on the whole package
docs: register Forward Study 2
ops: unhang the weekly backtest refresh
```

## Pull requests

CI must be green: lint, mypy, the test matrix (3.11–3.14), the dashboard suite,
the wheel build, and the secret scan. Describe what you verified and what you
could not. "Should work" is not a test result.

## Staged lint debt

`pyproject.toml` disables a handful of ruff rules with a comment explaining each.
Those are staged for removal, not waived: `B905` (zip strict), `B904` (raise
from), `UP042` (StrEnum, which would silently change JSON/Postgres
serialisation), `SIM115`. Do not add new violations of them — they are tolerated
only in code that predates linting.
