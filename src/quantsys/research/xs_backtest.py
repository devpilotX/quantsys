"""Cross-sectional, monthly-rebalanced factor backtester.

Honest by construction:
  * point-in-time universe (survivorship-free, from bhavcopy),
  * factor at month-end t uses only data <= t; returns earned AFTER t,
  * real costs via the engine's own `quantsys.costs.CostModel` (delivery equity),
  * daily equity curve so metrics reuse `quantsys.backtest.metrics` unchanged.

Between rebalances the book is held, not re-weighted: weights drift with
their own returns each day, and a rebalance pays for the full move from the
drifted book back to target. A month whose rebalance cannot be formed keeps
the held book earning.

Portfolios:
  * long_only  — equal-weight top-K by factor (carries market beta),
  * long_short — equal-weight top-K long / bottom-K short, dollar-neutral
                 (each side = leverage, so gross = 2 x leverage, net = 0): the
                 pure-alpha / market-neutral test.

The benchmark is the equal-weight daily return of the eligible universe. It
is rebalanced to equal weight every day at no cost, so it is a cost-free
equal-weight basket rather than buy-and-hold. realized_beta is the strategy's
beta to this benchmark over the backtest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from quantsys.backtest.metrics import compute_metrics, deflated_sharpe, monte_carlo_resample
from quantsys.config.schema import CostConfig
from quantsys.core.types import Instrument, InstrumentKind
from quantsys.costs import CostModel
from quantsys.research import factors as F

_E0 = 1_000_000.0
_MIN_BETA_OBS = 60


@dataclass(frozen=True)
class BTConfig:
    factor: tuple[str, ...] = ("momentum",)   # composite = mean of z-scores
    side: str = "long_short"                  # "long_short" | "long_only"
    top_k: int = 30
    top_n_universe: int = 200
    adv_window: int = 60
    leverage: float = 1.0
    min_price: float = 20.0


@dataclass
class BTResult:
    equity: pd.Series                          # daily, first point = capital at the first rebalance
    bench: pd.Series                           # daily equal-weight universe, same base
    metrics: dict = field(default_factory=dict)
    bench_metrics: dict = field(default_factory=dict)
    n_rebalances: int = 0
    avg_names: float = 0.0
    realized_beta: float | None = None         # daily strategy vs benchmark, over the backtest
    turnover_ann: float | None = None
    # Held name-days whose return was missing and was booked as 0: the true
    # outcome of those days is unknown, so a large count makes the curve suspect.
    missing_held_name_days: int = 0


def prepare_matrices(panel: pd.DataFrame) -> dict:
    """Precompute the wide matrices once so a config grid reuses them."""
    rets = F.daily_returns(panel)
    return {
        "rets": rets,
        "adjp": F.adjusted_prices(rets),
        "turnover": F.wide(panel, "turnover"),
        "close": F.wide(panel, "close"),
    }


def _equity_one_way_cost_frac(cost_model: CostModel) -> float:
    """One-way delivery-equity cost as a fraction of notional (taxes, slippage
    and brokerage). Brokerage is capped at Rs 20 an order, which is nothing
    at this notional, so the fraction barely depends on order size."""
    inst = Instrument(symbol="X", token="", exchange="NSE", kind=InstrumentKind.EQUITY,
                      lot_size=1, tick_size=0.05, point_value=1.0, sector="x",
                      adv=5_000_000, margin_rate=1.0)
    rt = cost_model.round_trip(inst, qty=10_000, price=500.0, delivery=True)
    return rt / (2.0 * 10_000 * 500.0)


def _target_book(cfg: BTConfig, rets, adjp, turnover, close,
                 t: pd.Timestamp) -> tuple[pd.Series, list[str]] | None:
    """Target weights and universe at month-end t, or None when the universe
    or the factor cross-section is too thin to form the book."""
    uni = F.eligible_universe(turnover, close, t, top_n=cfg.top_n_universe,
                              adv_window=cfg.adv_window, min_price=cfg.min_price)
    if len(uni) < cfg.top_k * 2:
        return None

    # composite factor = mean of component z-scores (already universe-restricted)
    zs = [F.compute_factor(name, rets, adjp, turnover, t, uni) for name in cfg.factor]
    zs = [z for z in zs if len(z)]
    if not zs:
        return None
    score = pd.concat(zs, axis=1).mean(axis=1).dropna().sort_values()
    if len(score) < cfg.top_k * 2:
        return None

    longs = score.tail(cfg.top_k).index
    w = pd.Series(0.0, index=uni, dtype=float)
    if cfg.side == "long_short":
        shorts = score.head(cfg.top_k).index
        w[longs] = +cfg.leverage / cfg.top_k
        w[shorts] = -cfg.leverage / cfg.top_k
    else:
        w[longs] = cfg.leverage / cfg.top_k
    return w, uni


def run_xs_backtest(panel: pd.DataFrame, cfg: BTConfig,
                    cost_model: CostModel | None = None,
                    start: str | None = None, end: str | None = None,
                    n_trials: int = 1, mc: bool = False,
                    prepared: dict | None = None) -> BTResult:
    cost_model = cost_model or CostModel(CostConfig())
    one_way = _equity_one_way_cost_frac(cost_model)

    P = prepared or prepare_matrices(panel)
    rets, adjp, turnover, close = P["rets"], P["adjp"], P["turnover"], P["close"]
    cal = rets.index
    rebal = [d for d in F.month_end_dates(cal)]
    end_ts = pd.Timestamp(end) if end else None

    w = pd.Series(dtype=float)       # held book as fractions of equity, drifted daily
    bench_uni: list[str] = []
    base_date: pd.Timestamp | None = None
    daily_port: list[tuple[pd.Timestamp, float]] = []
    daily_bench: list[tuple[pd.Timestamp, float]] = []
    turnovers: list[float] = []
    names_count: list[int] = []
    n_reb = 0
    n_missing = 0

    for i, t in enumerate(rebal[:-1]):
        nxt = rebal[i + 1]
        if start and t < pd.Timestamp(start):
            continue
        if end_ts is not None and t > end_ts:
            break
        target = _target_book(cfg, rets, adjp, turnover, close, t)
        cost_today = 0.0
        if target is not None:
            w_new, bench_uni = target
            names_count.append(int((w_new != 0).sum()))
            # transaction cost at rebalance, charged as an equity hit on the
            # first held day, on the trade from the DRIFTED book to target
            dw = float(w_new.subtract(w, fill_value=0.0).abs().sum())
            turnovers.append(dw)
            cost_today = dw * one_way
            w = w_new
            n_reb += 1
            if base_date is None:
                base_date = t
        elif base_date is None:
            continue                 # nothing held yet
        # otherwise the rebalance is skipped and the held book keeps earning

        # daily P&L over (t, nxt], never past `end`
        hold_days = cal[(cal > t) & (cal <= nxt)]
        if end_ts is not None:
            hold_days = hold_days[hold_days <= end_ts]
        first = True
        for d in hold_days:
            r_all = rets.loc[d]
            r = r_all.reindex(w.index)
            n_missing += int((r.isna() & (w != 0)).sum())
            r = r.fillna(0.0)
            book_ret = float((w * r).sum())
            daily_port.append((d, book_ret - (cost_today if first else 0.0)))
            daily_bench.append((d, float(r_all.reindex(bench_uni).fillna(0.0).mean())))
            # each position grows with its own return, equity with the book's
            w = w * (1.0 + r) / (1.0 + book_ret)
            first = False

    if not daily_port or base_date is None:
        return BTResult(pd.Series(dtype=float), pd.Series(dtype=float),
                        missing_held_name_days=n_missing)

    def _curve(pairs, e0=_E0):
        # base point at the first rebalance, so day one's return (and its
        # book-building cost) is part of the curve
        idx = [base_date] + [d for d, _ in pairs]
        rs = np.array([r for _, r in pairs])
        return pd.Series(e0 * np.concatenate([[1.0], np.cumprod(1.0 + rs)]), index=idx)

    eq = _curve(daily_port)
    bench = _curve(daily_bench)

    de = [(d, v) for d, v in zip(eq.index, eq.values)]
    db = [(d, v) for d, v in zip(bench.index, bench.values)]
    m = compute_metrics(de, [], 0.0, 0.0, float(eq.iloc[0]))
    if m.get("sharpe") is not None:
        m["deflated"] = deflated_sharpe(m["sharpe"], m.get("n_returns", 0),
                                        m.get("skew", 0.0), m.get("kurtosis", 3.0), n_trials)
        if mc:
            m["monte_carlo"] = monte_carlo_resample(de)
    bm = compute_metrics(db, [], 0.0, 0.0, float(bench.iloc[0]))

    # Realized beta of the strategy's daily returns to the benchmark's, one
    # ddof throughout. The old figure was the ex-ante beta of each month's new
    # weights over the formation window, and mixed ddof=1 cov with ddof=0 var.
    port_r = np.array([r for _, r in daily_port])
    mkt_r = np.array([r for _, r in daily_bench])
    realized_beta = None
    if len(port_r) > _MIN_BETA_OBS:
        mkt_var = float(np.var(mkt_r, ddof=1))
        if mkt_var > 0:
            realized_beta = float(np.cov(port_r, mkt_r, ddof=1)[0, 1] / mkt_var)

    return BTResult(
        equity=eq, bench=bench, metrics=m, bench_metrics=bm,
        n_rebalances=n_reb, avg_names=float(np.mean(names_count)) if names_count else 0.0,
        realized_beta=realized_beta,
        turnover_ann=float(np.mean(turnovers) * 12) if turnovers else None,
        missing_held_name_days=n_missing,
    )
