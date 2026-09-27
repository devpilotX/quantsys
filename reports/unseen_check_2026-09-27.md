# Unseen-data check, 2026-09-27

The alpha search closed on 2026-06-22 with data to 2026-06-19
([docs/RESEARCH_CLOSEOUT.md](../docs/RESEARCH_CLOSEOUT.md)). This note re-runs
the frozen rules, unchanged, on a panel rebuilt from NSE's public archive to
2026-09-25. Sessions from 2026-06-20 onward were seen by no rule, so they are
the only honest out-of-sample test available without new parameters.

Data: NSE cash bhavcopy 2016-01-01 to 2026-09-25 (4,560,174 rows) and the
F&O series over the same span (2,649 sessions). Intraday history was not
available for this check: it comes from Angel One and needs credentials that
were not on the machine, so the engine's 15-minute walk-forward was not re-run.
Weekly and monthly figures are resamples of the daily return streams.

## The 68 unseen sessions (2026-06-20 to 2026-09-25)

| Stream | Total | Sharpe (ann.) | Max drawdown |
|---|---:|---:|---:|
| S1 BANKNIFTY PCR | -6.9% | -5.28 | 7.2% |
| S2 market-neutral momentum | +3.5% | +0.67 | 11.4% |
| COMBO (inverse vol) | -4.4% | -2.36 | 6.1% |
| Top-100 equal-weight market | +6.6% | +2.27 | 2.3% |

68 sessions is too short to judge a Sharpe, so these rows are evidence of
direction only. The direction is against the sleeves: the combination lost
money while the market rose.

## The pre-registered combine test on the extended data

`python -m quantsys.research.run_combine`, no change to any parameter:

| Sleeve | IS Sharpe | OOS Sharpe | OOS deflated | P(SR<0) | Net CAGR | Max DD |
|---|---:|---:|---:|---:|---:|---:|
| S1 | 0.44 | 0.28 | 0.35 | 0.33 | 2.2% | 12.7% |
| S2 | 1.01 | 0.69 | 0.61 | 0.12 | 13.5% | 28.5% |
| COMBO | 0.83 | 0.62 | 0.56 | 0.16 | 5.3% | 8.1% |

The gate now fails on three of five tests (OOS Sharpe, deflated Sharpe,
P(SR<0)) where the June run failed on two. Three more months moved every
out-of-sample number down.

## Weekly and monthly horizons, OOS from 2024-01-01

| Stream | Weekly Sharpe | Monthly Sharpe | Total |
|---|---:|---:|---:|
| S1 | 0.27 | 0.29 | +5.9% |
| S2 | 0.77 | 0.72 | +41.7% |
| COMBO | 0.62 | 0.63 | +15.0% |
| Market | 0.99 | 1.01 | +49.9% |

No sleeve beats holding the market at any horizon, before or after costs.

## Forward Study 2 sleeves

`scripts/_study2_sanity_checks.py`, output in
[study2_sanity_2026-09-27.md](study2_sanity_2026-09-27.md): the reversal grid
is still negative in every cell (deployed cell Sharpe -0.74), and the
turn-of-month tilt still shows +12.3 bps a day in the window against +3.8
outside (t = 1.67, below significance). Down-shock is not re-run by its own
stop rule; its forward evidence comes from Forward Study 2.

## What it means

The closeout verdict stands and is slightly stronger: no price or volume rule
in this project beats the index out of sample. Re-tuning on the 2016 to 2026
sample would be fitting noise (PBO 0.84 there). The legitimate ways forward are
the ones already registered: read Forward Study 2 on 2027-01-05, or register a
new rule before it sees data.
