# Study 2 sanity checks — 2026-09-27

Panel: 2403 sessions x 48 config equities (2017-01-03..2026-09-25), net 28 bps RT delivery.

## Short-term reversal (frozen: lookback 5d, k=8, weekly)

| lookback | k | net Sharpe | net CAGR | frozen |
|---|---|---|---|---|
| 3 | 6 | -0.93 | -10.8% |  |
| 3 | 8 | -0.78 | -8.0% |  |
| 3 | 10 | -0.73 | -6.7% |  |
| 5 | 6 | -0.90 | -10.6% |  |
| 5 | 8 | -0.74 | -8.3% |  <-- deployed |
| 5 | 10 | -0.80 | -7.7% |  |
| 10 | 6 | -0.70 | -8.1% |  |
| 10 | 8 | -0.66 | -6.8% |  |
| 10 | 10 | -0.60 | -5.6% |  |

Frozen cell: Sharpe -0.74, deflated (n_trials=9) 0.000, grid PBO 0.79.

## Turn-of-month (frozen: -2/+3 weekdays, long index)

in-window mean +12.3 bps/d (n=541) vs outside +3.8 bps/d (n=1861), t=1.67; long-in-window-only Sharpe 0.88 (costs ~2 bps RT ignored).

## Down-shock (frozen z3.5/hold10)

Not re-run (stop-rule): full-gate record stands in docs/PILLAR4_EVENT_DRIVEN.md — hold-out Sharpe 1.75, deflated 0.46 (FAILED gate), PBO 0.31, MC P(SR<0) 0.002. Forward evidence only.