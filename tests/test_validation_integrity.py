"""Regression tests for the research-validation integrity fixes: PBO under ties
and at odd config counts, and the cross-sectional backtester's accounting
(weight drift, costs on the drifted book, skipped rebalances, the end clip,
missing returns and realized beta)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from quantsys.config.schema import CostConfig
from quantsys.costs import CostModel
from quantsys.research import factors as F
from quantsys.research import validation as V
from quantsys.research import xs_backtest as xs


# --------------------------------------------------------------- item 10: PBO
def test_pbo_is_one_half_when_every_config_is_identical():
    x = np.random.default_rng(0).standard_normal(400)
    out = V.pbo_cscv(np.column_stack([x] * 6), n_splits=10)
    assert out["pbo"] == pytest.approx(0.5)


def test_pbo_is_near_one_half_for_noise_at_an_odd_config_count():
    vals = [V.pbo_cscv(np.random.default_rng(s).standard_normal((240, 3)), n_splits=10)["pbo"]
            for s in range(30)]
    assert np.mean(vals) == pytest.approx(0.5, abs=0.06)


def test_pbo_does_not_depend_on_the_order_of_tied_configs():
    rng = np.random.default_rng(0)
    good = rng.standard_normal(400) * 0.01 + 0.0015
    noise = [rng.standard_normal(400) * 0.01 for _ in range(7)]
    first = V.pbo_cscv(np.column_stack([good] * 4 + noise), n_splits=10)["pbo"]
    last = V.pbo_cscv(np.column_stack(noise + [good] * 4), n_splits=10)["pbo"]
    assert first == pytest.approx(last)


def test_pbo_is_near_zero_for_a_planted_better_config():
    rng = np.random.default_rng(1)
    R = rng.standard_normal((400, 5)) * 0.01
    R[:, 2] += 0.02
    assert V.pbo_cscv(R, n_splits=10)["pbo"] < 0.05
    # duplicating the better config must not change the verdict
    dup = np.column_stack([R, R[:, 2], R[:, 2]])
    assert V.pbo_cscv(dup, n_splits=10)["pbo"] < 0.05


# ------------------------------------------------- item 12: xs accounting
# top/bottom 2 of 8 at leverage 1: (symbol, weight)
_BOOK = (("S6", 0.5), ("S7", 0.5), ("S0", -0.5), ("S1", -0.5))


def _prepared(n_days: int = 340, n_sym: int = 8, seed: int = 0) -> dict:
    """Wide matrices with the momentum ranking fixed by construction (S7
    strongest, S0 weakest) and independent random returns, so the book is
    known and the P&L it earns can be checked in closed form."""
    idx = pd.bdate_range("2020-01-01", periods=n_days)
    names = [f"S{j}" for j in range(n_sym)]
    t = np.arange(n_days)[:, None]
    adjp = pd.DataFrame(np.exp(1e-4 * t * np.arange(n_sym)), index=idx, columns=names)
    rets = pd.DataFrame(np.random.default_rng(seed).normal(0.0, 0.02, (n_days, n_sym)),
                        index=idx, columns=names)
    return {"rets": rets, "adjp": adjp,
            "turnover": pd.DataFrame(1e8, index=idx, columns=names),
            "close": pd.DataFrame(100.0, index=idx, columns=names)}


def _cfg() -> xs.BTConfig:
    return xs.BTConfig(factor=("momentum",), side="long_short", top_k=2, top_n_universe=8,
                       adv_window=20, min_price=1.0)


def _zero_costs() -> CostModel:
    return CostModel(CostConfig(
        brokerage_flat=0.0, brokerage_pct=0.0, stt_future_sell=0.0, stt_option_sell=0.0,
        stt_delivery=0.0, stt_intraday_sell=0.0, exch_equity=0.0, exch_future=0.0,
        exch_option=0.0, sebi_rate=0.0, stamp_delivery=0.0, stamp_intraday=0.0,
        stamp_future=0.0, stamp_option=0.0, gst=0.0,
        slippage_bps={"EQUITY": 0.0, "FUTURE": 0.0, "OPTION": 0.0, "INDEX": 0.0},
        impact_coeff=0.0))


def _run(P: dict, cm: CostModel | None = None, **kw) -> xs.BTResult:
    return xs.run_xs_backtest(pd.DataFrame(), _cfg(), cost_model=cm or _zero_costs(),
                              prepared=P, **kw)


def _periods(P: dict, first: str) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    rebal = F.month_end_dates(P["rets"].index)
    k = rebal.index(pd.Timestamp(first))
    return list(zip(rebal[k:-1], rebal[k + 1:]))


def _period_growth(P: dict, lo, hi) -> pd.Series:
    idx = P["rets"].index
    return (1.0 + P["rets"].loc[(idx > lo) & (idx <= hi)]).prod()


def _buy_and_hold(P: dict, periods, e0: float = 1_000_000.0) -> float:
    """Equity if the book is bought at each period start and left alone."""
    eq = e0
    for lo, hi in periods:
        g = _period_growth(P, lo, hi)
        eq *= 1.0 + sum(w * (g[s] - 1.0) for s, w in _BOOK)
    return eq


def test_xs_book_drifts_between_rebalances():
    P = _prepared()
    res = _run(P)
    assert res.n_rebalances == 4
    expected = _buy_and_hold(P, _periods(P, "2020-12-31"))
    assert res.equity.iloc[-1] == pytest.approx(expected, rel=1e-10)


def test_xs_rebalance_trades_and_pays_for_the_drift():
    P = _prepared()
    periods = _periods(P, "2020-12-31")
    turnover, first_day = [2.0], []           # the first rebalance builds the whole book
    for lo, hi in periods[:-1]:
        g = _period_growth(P, lo, hi)
        nav = 1.0 + sum(w * (g[s] - 1.0) for s, w in _BOOK)
        turnover.append(sum(abs(w - w * g[s] / nav) for s, w in _BOOK))
    idx = P["rets"].index
    for lo, _ in periods:
        d1 = idx[idx > lo][0]
        first_day.append(sum(w * P["rets"].loc[d1, s] for s, w in _BOOK))

    cm = CostModel(CostConfig())
    with_costs, free = _run(P, cm), _run(P)
    assert with_costs.turnover_ann == pytest.approx(np.mean(turnover) * 12, rel=1e-10)

    one_way = xs._equity_one_way_cost_frac(cm)
    ratio = np.prod([1.0 - dw * one_way / (1.0 + g1) for dw, g1 in zip(turnover, first_day)])
    assert with_costs.equity.iloc[-1] / free.equity.iloc[-1] == pytest.approx(ratio, rel=1e-10)


def test_xs_held_book_keeps_earning_through_a_skipped_rebalance():
    P = _prepared()
    # the 2021-01-29 universe collapses to two names, so that rebalance is skipped
    P["close"].loc[pd.Timestamp("2021-01-29"), [f"S{j}" for j in range(6)]] = 0.5
    res = _run(P)
    assert res.n_rebalances == 3

    idx = P["rets"].index
    held = idx[(idx > pd.Timestamp("2020-12-31"))]
    assert list(res.equity.index[1:]) == list(held), "skipped month dropped from the curve"
    periods = [(pd.Timestamp("2020-12-31"), pd.Timestamp("2021-02-26")),
               *_periods(P, "2021-02-26")]
    assert res.equity.iloc[-1] == pytest.approx(_buy_and_hold(P, periods), rel=1e-10)


def test_xs_last_holding_period_stops_at_end():
    res = _run(_prepared(), end="2021-02-15")
    assert res.equity.index.max() == pd.Timestamp("2021-02-15")
    assert res.bench.index.max() == pd.Timestamp("2021-02-15")


def test_xs_counts_held_name_days_with_missing_returns():
    P = _prepared()
    held_gap = pd.bdate_range("2021-01-11", periods=3)
    P["rets"].loc[held_gap, "S7"] = np.nan                  # in the book
    P["rets"].loc[pd.bdate_range("2021-01-18", periods=2), "S3"] = np.nan   # not in the book
    res = _run(P)
    assert res.missing_held_name_days == 3


def test_xs_realized_beta_is_measured_on_the_backtest_returns():
    res = _run(_prepared(seed=4), CostModel(CostConfig()))
    p = res.equity.pct_change().dropna().to_numpy()
    m = res.bench.pct_change().dropna().to_numpy()
    expected = np.cov(p, m, ddof=1)[0, 1] / np.var(m, ddof=1)
    assert res.realized_beta == pytest.approx(expected, rel=1e-10)


def test_xs_deflates_with_the_number_of_returns():
    from quantsys.backtest.metrics import deflated_sharpe

    res = xs.run_xs_backtest(pd.DataFrame(), _cfg(), cost_model=CostModel(CostConfig()),
                             prepared=_prepared(seed=2), n_trials=7)
    m = res.metrics
    n_returns = len(res.equity.pct_change().dropna())
    expected = deflated_sharpe(m["sharpe"], n_returns, m["skew"], m["kurtosis"], 7)
    assert m["deflated"] == pytest.approx(expected, rel=1e-12)


def test_xs_curves_start_from_capital_before_the_first_trade():
    P = _prepared()
    res = _run(P, CostModel(CostConfig()))
    assert res.equity.index[0] == pd.Timestamp("2020-12-31")
    assert res.equity.iloc[0] == 1_000_000.0
    assert res.bench.iloc[0] == 1_000_000.0
    # day one carries the cost of building the book, so it is in the metrics
    assert res.metrics["n_returns"] == len(res.equity) - 1
    assert res.metrics["total_return"] == pytest.approx(res.equity.iloc[-1] / 1_000_000.0 - 1)
