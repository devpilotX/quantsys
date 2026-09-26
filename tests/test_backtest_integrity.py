"""Regression tests for the backtest and statistics integrity fixes.

Each test pins a defect that made a reported number wrong: marks lost at a
walk-forward fold start, fills at a stale close, a CSV replay that stalled on a
duplicate row, trade statistics dominated by zero-fee partial closes, a
bootstrap that never drew the last return, metrics that dropped day one, the
pooled-IS and combined-OOS aggregates, the sweep's trial count and the
verdict's cost check. A scripted engine keeps the loop and broker tests free
of strategy logic.
"""

from __future__ import annotations

import csv
import logging
import math
import sys
from datetime import datetime, timedelta
from types import SimpleNamespace

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from quantsys.backtest import sensitivity
from quantsys.backtest import walkforward as wfmod
from quantsys.backtest.loop import BacktestResult, EquitySample, run_backtest
from quantsys.backtest.metrics import compute_metrics, deflated_sharpe, monte_carlo_resample
from quantsys.backtest.sensitivity import SensitivityResult, sweep
from quantsys.backtest.simbroker import SimBroker
from quantsys.backtest.synth import replay_bars, synthetic_bars
from quantsys.backtest.walkforward import WalkForwardResult, walk_forward
from quantsys.config import load_config
from quantsys.config.schema import CostConfig
from quantsys.core.types import (
    Bar,
    Decision,
    ExecutionStyle,
    Instrument,
    InstrumentKind,
    OrderIntent,
    RegimeState,
    Urgency,
)
from quantsys.costs import CostModel

CFG_PATH = "config/base.yaml"


# ------------------------------------------------------------------ helpers
def _loop_cfg() -> SimpleNamespace:
    """run_backtest reads only engine.decision_bar_minutes when given an engine."""
    return SimpleNamespace(engine=SimpleNamespace(decision_bar_minutes=15))


def _times(n: int, start: datetime = datetime(2024, 1, 1, 9, 15)) -> list[datetime]:
    return [start + timedelta(minutes=15 * i) for i in range(n)]


def _bar(ts: datetime, px: float) -> Bar:
    return Bar(ts, px, px, px, px, 1.0)


def _decision(ts: datetime, orders: tuple[OrderIntent, ...],
              kill_reason: str | None = None) -> Decision:
    return Decision(ts, 0.0, "T1", RegimeState("calm_range", {}, 1.0, {}), (), {},
                    1.0, 0.0, (), orders, kill_reason=kill_reason)


class _ScriptedEngine:
    """Just enough engine for run_backtest: issues pre-scheduled orders and
    records the equity it was shown. With kill_from set it behaves like the
    real kill switch, re-issuing a flatten for every open position each bar."""

    def __init__(self, symbols, plan=None, kill_from: datetime | None = None):
        self.instruments = {s: Instrument(s, kind=InstrumentKind.EQUITY) for s in symbols}
        self.cost_model = CostModel(CostConfig())
        self.plan = dict(plan or {})
        self.kill_from = kill_from
        self.seen: list[tuple[datetime, float]] = []

    def post_bar(self, state) -> None:
        pass

    def decide(self, state) -> Decision:
        self.seen.append((state.ts, state.equity))
        if self.kill_from is not None and state.ts >= self.kill_from:
            orders = tuple(
                OrderIntent(s, -p.qty, ExecutionStyle.MARKET_SINGLE, Urgency.KILL,
                            reason="kill_switch")
                for s, p in sorted(state.positions.items()) if p.qty
            )
            return _decision(state.ts, orders, kill_reason="max_drawdown")
        orders = tuple(
            OrderIntent(s, q, ExecutionStyle.MARKET_SINGLE, Urgency.NORMAL, "stub", "")
            for s, q in self.plan.get(state.ts, ())
        )
        return _decision(state.ts, orders)

    def end_warmup(self, positions) -> None:
        pass

    def state_dict(self) -> dict:
        return {}


def _daily_curve(values, start: datetime = datetime(2024, 1, 1)) -> list[tuple[datetime, float]]:
    return [(start + timedelta(days=i), float(v)) for i, v in enumerate(values)]


@pytest.fixture(scope="module")
def cfg():
    return load_config(CFG_PATH)


@pytest.fixture(scope="module")
def bars(cfg):
    syms = [u.symbol for u in cfg.universe]
    return list(synthetic_bars(syms, datetime(2026, 1, 1), 800,
                               cfg.engine.decision_bar_minutes, seed=5))


@pytest.fixture(scope="module")
def full_run(cfg, bars):
    return run_backtest(cfg, iter(bars), 50_000_000, warmup_bars=100)


@pytest.fixture(scope="module")
def wf_run(cfg, bars):
    """One real walk-forward, with the continuous broker and every shadow-IS
    result captured so the aggregates can be checked against their sources."""
    brokers: list[SimBroker] = []
    shadows: list[dict] = []
    real_broker, real_shadow = wfmod.SimBroker, wfmod._shadow_is

    def spy_broker(*a, **k):
        b = real_broker(*a, **k)
        brokers.append(b)
        return b

    def spy_shadow(*a, **k):
        m = real_shadow(*a, **k)
        shadows.append(m)
        return m

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(wfmod, "SimBroker", spy_broker)
        mp.setattr(wfmod, "_shadow_is", spy_shadow)
        wf = walk_forward(cfg, bars, 50_000_000, n_folds=4, train_frac=0.5)
    return SimpleNamespace(wf=wf, broker=brokers[0], shadows=shadows)


# ------------------------------------------------ item 2: replay duplicates
def _write_csv(path, rows) -> None:
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["ts", "open", "high", "low", "close", "volume"])
        for ts, px in rows:
            w.writerow([ts.isoformat(), px, px, px, px, 1])


def test_replay_keeps_streaming_a_symbol_after_a_duplicate_timestamp(tmp_path, caplog):
    ts = [datetime(2024, 1, 1 + i // 3, 9 + i % 3, 15) for i in range(9)]
    _write_csv(tmp_path / "A.csv", [(t, 100 + i) for i, t in enumerate(ts)])
    rows_b = [(t, 200 + i) for i, t in enumerate(ts)]
    rows_b.insert(2, (ts[1], 999))          # a re-sent bar for ts[1]
    _write_csv(tmp_path / "B.csv", rows_b)

    with caplog.at_level(logging.WARNING, logger="quantsys.backtest.synth"):
        out = list(replay_bars(str(tmp_path), ["A", "B"]))

    assert [t for t, _ in out] == ts
    assert all("B" in batch for _, batch in out), "B stalled after its duplicate row"
    assert out[1][1]["B"].close == 999      # the later row of the duplicate wins
    assert [b["B"].close for _, b in out[2:]] == [200 + i for i in range(2, 9)]
    assert "B" in caplog.text and "1 duplicate" in caplog.text


# ------------------------------------------- item 1: marks at a fold start
def test_continued_session_keeps_the_mark_of_a_held_symbol_without_a_bar():
    t = _times(4)
    eng = _ScriptedEngine(("A", "B"), plan={t[0]: [("B", 5000)]})
    brk = SimBroker(1_000_000, eng.instruments, eng.cost_model)
    run_backtest(_loop_cfg(), [(t[0], {"A": _bar(t[0], 100), "B": _bar(t[0], 100)})],
                 1_000_000, engine=eng, broker=brk)
    true_equity = brk.equity({"A": 100.0, "B": 100.0})

    eng.plan = {}
    fold2 = [(t[1], {"A": _bar(t[1], 100)}),            # B has not printed yet
             (t[2], {"A": _bar(t[2], 100)}),
             (t[3], {"A": _bar(t[3], 100), "B": _bar(t[3], 100)})]
    r2 = run_backtest(_loop_cfg(), fold2, true_equity, engine=eng, broker=brk)

    assert [s.equity for s in r2.equity_curve] == pytest.approx([true_equity] * 3)
    assert [e for _, e in eng.seen[1:]] == pytest.approx([true_equity] * 3)


def test_walk_forward_fold_start_does_not_mark_a_held_symbol_at_zero(monkeypatch):
    syms = ("A", "B")
    stream = list(synthetic_bars(list(syms), datetime(2026, 1, 5), 80, 15, seed=2))
    # 80 bars, 2 folds: fold 0 scores bars 40-59, fold 1 scores 60-79. B does
    # not print on the first two bars of fold 1.
    for k in (60, 61):
        ts, batch = stream[k]
        stream[k] = (ts, {"A": batch["A"]})
    buy_ts = stream[40][0]
    qty = int(500_000 / stream[40][1]["B"].close)

    def factory(_cfg):
        return _ScriptedEngine(syms, plan={buy_ts: [("B", qty)]})

    monkeypatch.setattr(wfmod, "DecisionEngine", factory)
    wf = walk_forward(_loop_cfg(), stream, 1_000_000, n_folds=2, train_frac=0.5)

    curve = [s.equity for s in wf.oos_curve]
    assert len(curve) == 40
    # no bar for B and no fill on 60/61, so equity cannot move from bar 59
    assert curve[20] == pytest.approx(curve[19], rel=1e-12)
    assert curve[21] == pytest.approx(curve[19], rel=1e-12)


def test_mtm_refuses_to_value_a_held_position_without_a_mark():
    eng = _ScriptedEngine(("A", "B"))
    brk = SimBroker(1_000_000, eng.instruments, eng.cost_model)
    ts = datetime(2024, 1, 1, 9, 15)
    order = OrderIntent("B", 10, ExecutionStyle.MARKET_SINGLE, Urgency.NORMAL, "stub", "")
    brk.execute(_decision(ts, (order,)), {"B": 100.0}, ts)
    with pytest.raises(RuntimeError, match="B"):
        brk.mtm({"A": 100.0})
    with pytest.raises(RuntimeError, match="B"):
        brk.exposures({"A": 100.0})


# ---------------------------------------------- item 3: no stale-close fill
def test_order_for_a_symbol_without_a_bar_at_the_decision_is_not_filled():
    t = _times(3)
    stream = [(t[0], {"A": _bar(t[0], 100), "B": _bar(t[0], 100)}),
              (t[1], {"A": _bar(t[1], 110)}),                   # no B bar
              (t[2], {"A": _bar(t[2], 110), "B": _bar(t[2], 110)})]
    eng = _ScriptedEngine(("A", "B"), plan={t[1]: [("B", 1000)]})
    brk = SimBroker(1_000_000, eng.instruments, eng.cost_model)
    r = run_backtest(_loop_cfg(), stream, 1_000_000, engine=eng, broker=brk)

    assert "B" not in brk.positions, "filled at B's close from the previous bar"
    assert brk.n_fills == 0
    assert r.unfilled_no_bar == 1
    assert [s.equity for s in r.equity_curve] == pytest.approx([1_000_000] * 3)


def test_kill_flatten_waits_for_the_next_bar_of_the_symbol():
    t = _times(4)
    stream = [(t[0], {"A": _bar(t[0], 100), "B": _bar(t[0], 100)}),
              (t[1], {"A": _bar(t[1], 100)}),                   # kill fires; no B bar
              (t[2], {"A": _bar(t[2], 100), "B": _bar(t[2], 90)}),
              (t[3], {"A": _bar(t[3], 100), "B": _bar(t[3], 95)})]
    eng = _ScriptedEngine(("A", "B"), plan={t[0]: [("B", 1000)]}, kill_from=t[1])
    brk = SimBroker(1_000_000, eng.instruments, eng.cost_model)
    r = run_backtest(_loop_cfg(), stream, 1_000_000, engine=eng, broker=brk)

    assert "B" not in brk.positions
    assert len(brk.trades) == 1
    assert brk.trades[0].exit_ts == t[2]
    assert brk.trades[0].exit_price == 90.0     # the first B print after the kill
    assert r.unfilled_no_bar == 1


# ---------------------------------------- item 5: one record per round trip
def _broker(point_value: float = 1.0, kind: InstrumentKind = InstrumentKind.EQUITY):
    inst = Instrument("X", kind=kind, point_value=point_value)
    brk = SimBroker(10_000_000, {"X": inst}, CostModel(CostConfig()))
    return brk, inst


def _fill(brk: SimBroker, k: int, qty: int, px: float) -> None:
    ts = datetime(2024, 1, 1, 9, 15) + timedelta(minutes=15 * k)
    order = OrderIntent("X", qty, ExecutionStyle.MARKET_SINGLE, Urgency.NORMAL, "stub", "")
    brk.execute(_decision(ts, (order,)), {"X": px}, ts)


def _fee(brk: SimBroker, inst: Instrument, qty: int, px: float) -> float:
    return brk.cost_model.order_cost(inst, abs(qty), px, qty > 0,
                                     delivery=(inst.kind == InstrumentKind.EQUITY)).total


def test_adds_and_partial_closes_accumulate_into_one_round_trip():
    brk, inst = _broker()
    for k, (q, px) in enumerate([(100, 100.0), (50, 110.0), (-60, 120.0), (-90, 105.0)]):
        _fill(brk, k, q, px)

    assert len(brk.trades) == 1, [(t.qty, t.fees) for t in brk.trades]
    t = brk.trades[0]
    assert t.qty == 150
    assert t.entry_price == pytest.approx((100 * 100.0 + 50 * 110.0) / 150)
    assert t.exit_price == pytest.approx((60 * 120.0 + 90 * 105.0) / 150)
    assert t.fees == pytest.approx(brk.total_fees)
    assert t.pnl == pytest.approx(150 * (t.exit_price - t.entry_price))


def test_flip_splits_the_flipping_fee_between_the_two_round_trips():
    brk, inst = _broker(point_value=2.0, kind=InstrumentKind.FUTURE)
    _fill(brk, 0, 100, 100.0)
    _fill(brk, 1, -150, 110.0)      # closes 100, opens a 50 short
    _fill(brk, 2, 50, 105.0)
    f1, f2, f3 = (_fee(brk, inst, 100, 100.0), _fee(brk, inst, -150, 110.0),
                  _fee(brk, inst, 50, 105.0))

    assert len(brk.trades) == 2
    long_rt, short_rt = brk.trades
    assert long_rt.qty == 100 and short_rt.qty == -50
    assert long_rt.fees == pytest.approx(f1 + f2 * 100 / 150)
    assert short_rt.fees == pytest.approx(f2 * 50 / 150 + f3)
    assert long_rt.pnl == pytest.approx(100 * 10.0 * 2.0)
    assert short_rt.pnl == pytest.approx(-50 * (105.0 - 110.0) * 2.0)


_fills = st.lists(
    st.tuples(st.integers(-300, 300).filter(lambda q: q != 0),
              st.floats(50.0, 150.0, allow_nan=False, allow_infinity=False)),
    min_size=1, max_size=25,
)


@settings(max_examples=150, deadline=None)
@given(_fills)
def test_trade_records_are_round_trips_and_keep_the_equity_identity(fills):
    brk, inst = _broker(point_value=2.0, kind=InstrumentKind.FUTURE)
    start = brk.cash
    pos, round_trips = 0, 0
    expected_cash = start
    fills = list(fills)
    net = sum(q for q, _ in fills)
    if net:
        fills.append((-net, fills[-1][1]))      # end flat so every lot is a round trip
    for k, (q, px) in enumerate(fills):
        _fill(brk, k, q, px)
        expected_cash -= q * px * inst.point_value + _fee(brk, inst, q, px)
        new = pos + q
        if pos != 0 and (new == 0 or (new > 0) != (pos > 0)):
            round_trips += 1
        pos = new

    # cash is a function of the fills alone: the trade-record rework must not touch it
    assert brk.cash == pytest.approx(expected_cash, rel=1e-12, abs=1e-6)
    assert brk.positions == {}
    assert len(brk.trades) == round_trips
    for t in brk.trades:
        assert t.fees > 0
        assert t.pnl == pytest.approx(t.qty * (t.exit_price - t.entry_price) * inst.point_value,
                                      rel=1e-9, abs=1e-6)
    # the records reconcile exactly with the broker's cash and fee totals
    assert sum(t.fees for t in brk.trades) == pytest.approx(brk.total_fees, rel=1e-9)
    assert sum(t.pnl - t.fees for t in brk.trades) == pytest.approx(brk.cash - start,
                                                                    rel=1e-9, abs=1e-6)


def test_real_run_emits_one_fee_bearing_record_per_round_trip(full_run):
    trades = full_run.trades
    assert trades
    keys = [(t.symbol, t.entry_ts) for t in trades]
    assert len(keys) == len(set(keys)), "partial closes emitted as separate trades"
    assert all(t.fees > 0 for t in trades)


# ------------------------------------------- item 4: circular bootstrap
def test_bootstrap_draws_the_last_return_like_any_other():
    rng = np.random.default_rng(3)
    rets = rng.normal(0.002, 0.001, 60)
    rets[-1] = -0.30                                  # crash on the last day

    def curve(r):
        return _daily_curve(1e6 * np.concatenate([[1.0], np.cumprod(1 + r)]))

    at_end = monte_carlo_resample(curve(rets), n_paths=2000, block=5, seed=11)
    mid = monte_carlo_resample(curve(np.roll(rets, -29)), n_paths=2000, block=5, seed=11)

    assert at_end["p_sharpe_negative"] > 0.5
    assert at_end["max_dd_p95"] >= 0.29
    # a circular bootstrap is invariant to rotating the sample
    assert at_end["p_sharpe_negative"] == pytest.approx(mid["p_sharpe_negative"], abs=0.05)


# --------------------------------------- item 13: zero-variance Sharpe
def test_constant_return_curve_has_no_sharpe():
    de = _daily_curve(1e6 * 1.001 ** np.arange(40))
    m = compute_metrics(de, [], 0.0, 0.0, 1e6)
    assert m["sharpe"] is None
    assert "skew" not in m
    mc = monte_carlo_resample(de, n_paths=200)
    assert mc["sharpe_p05"] is None
    assert mc["insufficient_data"] is True
    assert mc["p_sharpe_negative"] == 1.0


# -------------------------------------------- item 8: day one and years
def test_metrics_count_the_first_day_against_starting_equity():
    d0 = datetime(2024, 1, 1)
    de = _daily_curve([95.0, 95.0, 95.0, 95.0], start=d0 + timedelta(days=1))
    m = compute_metrics(de, [], 0.0, 0.0, 100.0, start=d0)
    assert m["total_return"] == pytest.approx(-0.05)
    assert m["max_dd"] == pytest.approx(0.05)
    assert m["n_returns"] == 4


def test_cagr_exponent_counts_returns_not_points():
    de = _daily_curve(100 * 1.001 ** np.arange(253))        # 252 returns
    m = compute_metrics(de, [], 0.0, 0.0, 100.0)
    assert m["cagr"] == pytest.approx(1.001 ** 252 - 1, rel=1e-9)


def test_walk_forward_fold_return_is_measured_from_the_fold_start(monkeypatch):
    syms = ("A", "B")
    # 300 bars, 2 folds: fold 0 scores bars 150-224, three sessions of 15-min bars
    stream = list(synthetic_bars(list(syms), datetime(2026, 1, 5), 300, 15, seed=4))
    buy_ts = stream[150][0]                  # first scored bar of fold 0

    def factory(_cfg):
        return _ScriptedEngine(syms, plan={buy_ts: [("B", 500)]})

    monkeypatch.setattr(wfmod, "DecisionEngine", factory)
    wf = walk_forward(_loop_cfg(), stream, 1_000_000, n_folds=2, train_frac=0.5)

    fold0_end = wf.oos_curve[74].equity
    assert wf.folds[0].oos_metrics["total_return"] == pytest.approx(fold0_end / 1_000_000 - 1)
    assert wf.combined_oos["total_return"] == pytest.approx(wf.oos_curve[-1].equity / 1_000_000 - 1)


# ---------------------------------------------- items 6 and 7: aggregates
def test_combined_oos_costs_come_from_the_broker(wf_run):
    oos, brk = wf_run.wf.combined_oos, wf_run.broker
    assert brk.n_fills > 0, "the walk-forward never traded, so this proves nothing"
    assert oos["total_fees"] == pytest.approx(brk.total_fees, rel=1e-12)
    assert oos["traded_notional"] == pytest.approx(brk.traded_notional, rel=1e-12)
    assert oos["cost_drag_bps"] == pytest.approx(brk.total_fees / brk.traded_notional * 1e4,
                                                 rel=1e-12)


def test_pooled_is_uses_within_run_returns_once_per_date(wf_run):
    # oracle: each date's return comes from the earliest shadow run covering it
    first: dict = {}
    for m in wf_run.shadows:
        daily = m.get("_daily", [])
        for (_, v0), (d1, v1) in zip(daily, daily[1:]):
            first.setdefault(d1, v1 / v0 - 1.0)
    rets = np.array([first[d] for d in sorted(first)])
    pooled = wf_run.wf.pooled_is

    assert pooled["n_days"] == len(rets)
    assert pooled["sharpe"] == pytest.approx(rets.mean() / rets.std(ddof=1) * math.sqrt(252),
                                             rel=1e-9)
    # a stitched return series has no single book behind it
    assert "total_fees" not in pooled and "n_trades" not in pooled


# ---------------------------------------- item 9: trials and T in the DSR
def _fake_run(fail_on: tuple = (), distinct: bool = False):
    def fake(cfg, bars, starting_equity, warmup_bars=0):
        v = cfg.sizing.base_risk_frac
        if v in fail_on:
            raise ValueError(f"variant {v} rejected")
        res = BacktestResult(starting_equity=starting_equity)
        step = v if distinct else 0.001
        for d in range(6):
            eq = starting_equity * (1 + step * d + 0.0005 * (d % 2))
            res.equity_curve.append(
                EquitySample(datetime(2024, 1, 1, 15, 15) + timedelta(days=d), eq, eq, 0.0, 0.0))
        return res
    return fake


def test_sweep_counts_variants_that_fail_to_configure(cfg, monkeypatch, caplog):
    monkeypatch.setattr(sensitivity, "run_backtest", _fake_run(distinct=True))
    with caplog.at_level(logging.WARNING, logger="quantsys.backtest.sensitivity"):
        res = sweep(cfg, [], 1_000_000, {"sizing.base_risk_frac": [0.003, "bad", 0.005]})
    assert res.n_trials == 3
    assert len(res.points) == 2
    assert "bad" in caplog.text


def test_sweep_lets_an_error_from_the_run_propagate(cfg, monkeypatch):
    """A ValueError raised by the engine on one variant is a bug, not a
    failed trial: recorded as one, the study carried on with a clean log."""
    monkeypatch.setattr(sensitivity, "run_backtest", _fake_run(fail_on=(0.004,), distinct=True))
    with pytest.raises(ValueError, match=r"variant 0\.004 rejected"):
        sweep(cfg, [], 1_000_000, {"sizing.base_risk_frac": [0.003, 0.004, 0.005]})


def test_sweep_raises_when_every_variant_fails(cfg, monkeypatch):
    monkeypatch.setattr(sensitivity, "run_backtest", _fake_run(distinct=True))
    with pytest.raises(RuntimeError, match="every"):
        sweep(cfg, [], 1_000_000, {"sizing.base_risk_frac": ["bad", "worse"]})


def test_sweep_warns_on_identical_variants_but_still_counts_them(cfg, monkeypatch, caplog):
    monkeypatch.setattr(sensitivity, "run_backtest", _fake_run(distinct=False))
    with caplog.at_level(logging.WARNING, logger="quantsys.backtest.sensitivity"):
        res = sweep(cfg, [], 1_000_000, {"sizing.base_risk_frac": [0.003, 0.005]})
    assert res.n_trials == 2
    assert "identical" in caplog.text


def test_runstudy_deflates_with_the_number_of_oos_returns(monkeypatch):
    from quantsys.backtest import runstudy

    t0 = datetime(2024, 1, 1, 15, 15)
    oos_curve = [EquitySample(t0 + timedelta(days=d), 1e6 + 1e3 * d, 1e6, 0.0, 0.0)
                 for d in range(21)]
    oos = {"sharpe": 1.5, "n_days": 21, "n_returns": 20, "skew": 0.0, "kurtosis": 3.0,
           "cagr": 0.1}
    wf = WalkForwardResult(oos_curve=oos_curve, combined_oos=oos)
    captured: dict = {}

    def fake_persist(args, source, cfg, full, full_m, wf, oos, dsr, mc, n_trials, verdict):
        captured.update(dsr=dsr, n_trials=n_trials)

    monkeypatch.setattr(runstudy, "walk_forward", lambda *a, **k: wf)
    monkeypatch.setattr(runstudy, "sweep", lambda *a, **k: SensitivityResult(n_trials=5))
    monkeypatch.setattr(runstudy, "run_backtest",
                        lambda *a, **k: BacktestResult(starting_equity=1e6))
    monkeypatch.setattr(runstudy, "_persist", fake_persist)
    monkeypatch.setattr(sys, "argv", ["runstudy", "--synthetic", "--bars", "30", "--persist"])
    runstudy.main()

    assert captured["n_trials"] == 5
    assert captured["dsr"] == pytest.approx(deflated_sharpe(1.5, 20, 0.0, 3.0, 5), rel=1e-12)


def test_runstudy_prints_the_sweep_failures_beside_n_trials(monkeypatch, capsys):
    """sw.failures was never shown, so a variant the config refused counted
    as a trial without a trace in the report."""
    from quantsys.backtest import runstudy

    t0 = datetime(2024, 1, 1, 15, 15)
    oos_curve = [EquitySample(t0 + timedelta(days=d), 1e6 + 1e3 * d, 1e6, 0.0, 0.0)
                 for d in range(21)]
    wf = WalkForwardResult(oos_curve=oos_curve, combined_oos={
        "sharpe": 1.5, "n_days": 21, "n_returns": 20, "skew": 0.0, "kurtosis": 3.0})
    failed = "sizing.base_risk_frac='bad': ValueError('could not convert')"
    monkeypatch.setattr(runstudy, "walk_forward", lambda *a, **k: wf)
    monkeypatch.setattr(runstudy, "sweep",
                        lambda *a, **k: SensitivityResult(n_trials=3, failures=[failed]))
    monkeypatch.setattr(runstudy, "run_backtest",
                        lambda *a, **k: BacktestResult(starting_equity=1e6))
    monkeypatch.setattr(sys, "argv", ["runstudy", "--synthetic", "--bars", "30"])
    runstudy.main()
    shown = [line for line in capsys.readouterr().out.splitlines() if failed in line]
    assert len(shown) == 1 and "n_trials=3" in shown[0]


# ------------------------------------------------ item 11: verdict cost check
def test_verdict_refuses_an_edge_that_loses_money_net_of_costs():
    from quantsys.backtest import runstudy

    mc = {"p_sharpe_negative": 0.02}
    oos = {"sharpe": 1.2, "cagr": -0.01, "cost_drag_bps": 12.0}
    assert "Do NOT go live" in runstudy._verdict(False, oos, 0.99, mc)
    oos_unknown = {"sharpe": 1.2, "cagr": None, "cost_drag_bps": 12.0}
    assert "Do NOT go live" in runstudy._verdict(False, oos_unknown, 0.99, mc)


def test_verdict_still_passes_a_run_that_clears_every_criterion():
    from quantsys.backtest import runstudy

    mc = {"p_sharpe_negative": 0.02}
    oos = {"sharpe": 1.2, "cagr": 0.08, "cost_drag_bps": 12.0}
    assert "Eligible for tiny-capital" in runstudy._verdict(False, oos, 0.99, mc)
    # every other criterion still binds
    assert "Do NOT go live" in runstudy._verdict(False, {**oos, "sharpe": 0.8}, 0.99, mc)
    assert "Do NOT go live" in runstudy._verdict(False, oos, 0.95, mc)
    assert "Do NOT go live" in runstudy._verdict(False, oos, 0.99, {"p_sharpe_negative": 0.10})


# ------------------------------------- warm-up stop state at the boundary
def test_first_executed_bar_does_not_inherit_warm_up_stops(cfg, bars):
    """Warm-up decides targets that are never filled, and the stop tracker
    keeps an entry per target. Unless dropped at the boundary, the first
    executed bars measure stops from warm-up prices and fire them early."""
    seen: list[set[str]] = []
    engine = wfmod.DecisionEngine(cfg)
    real_decide = engine.decide
    warmup = 700                                      # past trend's 540-bar lookback

    def spy(state):
        # stop entries as the bar's decision starts, before it refreshes them
        seen.append({e["symbol"] for e in engine.risk.stops.entries.values()})
        return real_decide(state)

    engine.decide = spy
    run_backtest(cfg, iter(bars[:warmup + 5]), 50_000_000, warmup_bars=warmup, engine=engine)
    assert any(seen[:warmup]), "precondition: warm-up must have registered stops"
    assert seen[warmup] == set(), f"warm-up stops reached the first live bar: {sorted(seen[warmup])}"
