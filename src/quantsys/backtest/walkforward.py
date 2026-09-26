"""Walk-forward evaluation.

This engine is online/adaptive (EWMA edge stats, regime HMM, tier ladder) — it
has no separate "fit" step to freeze. So walk-forward here measures the honest
thing: train windows let the engine's estimators converge with NO execution
(in-sample warm-up), then the immediately-following test window executes and is
scored OUT-OF-SAMPLE on data the estimators had not yet seen when each decision
was made. Engine state carries forward across the rolling windows exactly as it
would live — there is no re-initialisation that would leak future info backward.

Returns per-fold OOS metrics plus the concatenated OOS curve, and a train-window
reference the dashboard renders beside it (labelled "is" for compatibility).
That reference is NOT an in-sample fit: it is a fresh engine run causally over
each fold's train window, so it differs from OOS in period and in warm-up (cold
start versus the carried engine), not in whether the estimators saw the data
they traded. Rolling train windows overlap each other and earlier test windows,
so the two series also share calendar dates. Read the gap as that, not as
overfitting.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from quantsys.backtest.loop import EquitySample, run_backtest
from quantsys.backtest.metrics import compute_metrics
from quantsys.backtest.simbroker import SimBroker
from quantsys.config.schema import AppConfig
from quantsys.core.types import Bar
from quantsys.engine.decision import DecisionEngine

# Book-level figures a stitched return series cannot have: it is pooled from
# several shadow runs, so no single broker's fees, notional or trades sit behind it.
_NO_BOOK_KEYS = ("n_trades", "total_fees", "traded_notional", "cost_drag_bps",
                 "turnover_x_per_year", "fee_drag_on_capital_ann")


@dataclass
class Fold:
    index: int
    train_start: datetime
    test_start: datetime
    test_end: datetime
    is_metrics: dict      # shadow run over the train window (see module docstring)
    oos_metrics: dict


@dataclass
class WalkForwardResult:
    folds: list[Fold] = field(default_factory=list)
    oos_curve: list[EquitySample] = field(default_factory=list)
    oos_trades: list = field(default_factory=list)
    combined_oos: dict = field(default_factory=dict)
    pooled_is: dict = field(default_factory=dict)
    # Equity just before the first OOS bar: the base of the first OOS return.
    oos_start: datetime | None = None
    oos_start_equity: float | None = None

    def oos_daily_equity(self) -> list[tuple[datetime, float]]:
        """The series combined_oos is computed from: the base point (stamped
        with the first OOS bar), then the last OOS equity of each date."""
        daily = _daily(self.oos_curve)
        if self.oos_start is None or self.oos_start_equity is None or not daily:
            return daily
        return [(self.oos_start, self.oos_start_equity), *daily]


def walk_forward(
    cfg: AppConfig,
    all_bars: list[tuple[datetime, dict[str, Bar]]],
    starting_equity: float,
    n_folds: int = 4,
    train_frac: float = 0.5,
) -> WalkForwardResult:
    """Rolling-origin folds over a materialised bar list.

    Each fold: [train window | test window]. Engine + broker state carry across
    folds (continuous, like live). Within a fold the train portion is warm-up
    (no fills); the test portion executes and is scored OOS.
    """
    n = len(all_bars)
    if n < n_folds * 20:
        raise ValueError("not enough bars for the requested folds")

    test_span = int(n * (1 - train_frac) / n_folds)
    train_span = int(n * train_frac)
    res = WalkForwardResult()

    # one continuous engine/broker across the whole horizon
    engine = DecisionEngine(cfg)
    broker = SimBroker(starting_equity, engine.instruments, engine.cost_model)

    shadow_runs: list[list[tuple[datetime, float]]] = []
    oos_fees = oos_notional = 0.0
    cursor = 0
    fed_hi = 0  # bars already streamed into the continuous engine/broker
    for fold_i in range(n_folds):
        train_lo = cursor
        test_lo = min(train_lo + train_span, n - test_span)
        test_hi = min(test_lo + test_span, n)
        if test_lo >= test_hi:
            break

        # Stream ONLY the bars not yet seen by the continuous engine. The engine
        # and broker already carry the earlier bars forward (exactly as live), so
        # re-feeding the overlapping train window would append those bars to the
        # carried BarHistory a second time — bloating it ~2.5x with DUPLICATE
        # bars. That both corrupts the estimators (regime HMM / EWMA cov see
        # repeated data) and makes per-bar regime/cov recompute blow up
        # super-linearly, which is what made walk_forward effectively hang.
        window = all_bars[fed_hi:test_hi]
        score_from = all_bars[test_lo][0]

        eq_before = broker.equity(_last_prices(all_bars, test_lo))
        fold_res = run_backtest(
            cfg, window, starting_equity=eq_before,
            engine=engine, broker=broker, score_from=score_from,
        )
        oos = compute_metrics(
            fold_res.daily_equity(), fold_res.trades, fold_res.total_fees,
            fold_res.traded_notional, eq_before, start=score_from,
        )
        # Train-window reference: a fresh engine and broker executing the train
        # bars causally from a cold start. It is neither in-sample-fitted nor
        # optimistic by construction; see the module docstring.
        is_metrics = _shadow_is(cfg, all_bars[train_lo:test_lo], eq_before)

        res.folds.append(Fold(
            index=fold_i,
            train_start=all_bars[train_lo][0],
            test_start=score_from,
            test_end=all_bars[test_hi - 1][0],
            is_metrics=is_metrics,
            oos_metrics=oos,
        ))
        if res.oos_start is None:
            res.oos_start, res.oos_start_equity = score_from, eq_before
        res.oos_curve.extend(fold_res.equity_curve)
        res.oos_trades.extend(fold_res.trades)
        # Broker deltas, not trade records: records miss the fees of lots still
        # open at the end and carry only one side of the traded notional.
        oos_fees += fold_res.total_fees
        oos_notional += fold_res.traded_notional
        if is_metrics.get("_daily"):
            shadow_runs.append(is_metrics["_daily"])

        fed_hi = test_hi
        cursor += test_span

    if res.oos_curve and res.oos_start_equity is not None:
        res.combined_oos = compute_metrics(
            _daily(res.oos_curve), res.oos_trades, oos_fees, oos_notional,
            res.oos_start_equity, start=res.oos_start,
        )
    pooled = _pool_returns(shadow_runs)
    if pooled:
        curve, level = [], 1.0
        for d, r in pooled:
            level *= 1.0 + r
            curve.append((d, level))
        res.pooled_is = compute_metrics(curve, [], 0.0, 0.0, 1.0, start=shadow_runs[0][0][0])
        for k in _NO_BOOK_KEYS:
            res.pooled_is.pop(k, None)
    return res


def _pool_returns(runs: list[list[tuple[datetime, float]]]) -> list[tuple[datetime, float]]:
    """Daily returns of several runs pooled into one dated series.

    Only within-run returns are used: each is the ratio of two consecutive
    points of the same run, so no return spans two runs (their equity levels
    are unrelated). The shadow windows overlap, so each date takes its return
    from the earliest run that covers it and no date repeats."""
    by_date: dict[datetime, float] = {}
    for daily in runs:
        for (_, v0), (d1, v1) in zip(daily, daily[1:]):
            by_date.setdefault(d1, v1 / v0 - 1.0)
    return sorted(by_date.items())


def _shadow_is(cfg: AppConfig, train_bars: list, starting_equity: float) -> dict:
    """Train-window reference: a fresh engine executing the train window from
    a cold start. Every decision is causal, so this is not in-sample-optimistic;
    it is the same engine on an earlier period with no warm-up. `_daily` holds
    its daily curve, base point first, for pooling."""
    if len(train_bars) < 20:
        return {"insufficient_data": True}
    eng = DecisionEngine(cfg)
    brk = SimBroker(starting_equity, eng.instruments, eng.cost_model)
    r = run_backtest(cfg, train_bars, starting_equity, engine=eng, broker=brk)
    m = compute_metrics(r.daily_equity(), r.trades, r.total_fees,
                        r.traded_notional, starting_equity, start=r.scored_from)
    daily = r.daily_equity()
    m["_daily"] = [(r.scored_from, starting_equity), *daily] if r.scored_from else daily
    return m


def _daily(curve: list[EquitySample]) -> list[tuple[datetime, float]]:
    """Last equity per session date (date carried as datetime midnight)."""
    out: dict = {}
    for s in curve:
        out[s.ts.date()] = s.equity
    return [(datetime(d.year, d.month, d.day), v) for d, v in sorted(out.items())]


def _last_prices(all_bars: list, upto: int) -> dict[str, float]:
    px: dict[str, float] = {}
    for _, batch in all_bars[:upto]:
        for sym, bar in batch.items():
            px[sym] = bar.close
    return px
