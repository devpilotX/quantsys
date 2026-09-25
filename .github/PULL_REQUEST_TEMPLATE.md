## What and why

<!-- What was wrong, what the consequence was, what this does about it.
     A description that only restates the diff is not useful. -->

## Type of change

- [ ] Bug fix affecting a reported number or an order quantity
- [ ] Bug fix, no numerical impact
- [ ] New capability
- [ ] Refactor with no behaviour change
- [ ] Tooling, CI, or documentation

## Verification

<!-- State what you ran and what it said. "Should work" is not a result. -->

- [ ] `python -m pytest tests -q` — result:
- [ ] `python -m ruff check .` — result:
- [ ] `python -m mypy` — result:
- [ ] Dashboard suite, if touched — result:
- [ ] A regression test exists that fails without this change

What I could not verify, and why:

## Numerical-integrity checklist

Tick only what applies; delete the rest. These are the failure modes that have
actually occurred in this repository.

- [ ] No look-ahead: a decision on bar `t` uses only information available at `t`
- [ ] Any cost charged in the cost gate is also charged on the fill
- [ ] No `float(x) or default` (NaN is truthy, so the default never applies)
- [ ] Failures are logged and narrow, not swallowed by a bare `except Exception`
- [ ] Every risk cap shrinks exposure; the vol targeter still runs before the caps
- [ ] Multi-leg groups scale jointly — no cap can orphan one leg of a hedge
- [ ] Tests are order-independent (no module-level mutable state)
- [ ] Reported performance numbers in `docs/` are updated, or explicitly noted as stale

## Research integrity

- [ ] This does not tune a strategy on the closed 2017–2026 sample
      (PBO there is 0.84; further tuning manufactures a false edge — see
      `docs/RESEARCH_CLOSEOUT.md`)
- [ ] Any new strategy work is a separately pre-registered, forward-only study

## Safety

- [ ] No credential, token, or TOTP seed added to tracked files
- [ ] `QS_LIVE_ARMED` is not set to a truthy value anywhere in this diff
- [ ] Live-arming chain is not weakened
