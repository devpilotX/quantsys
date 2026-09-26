"""Engine-level regressions for the decision pipeline: the kill and halt paths,
throttle ordering, cap-driven order flow, min-lot promotion, sleeve toggles
and warm-up stop state.

A fixed-signal stub stands in for the strategy ensemble so each test controls
the signals exactly. Everything downstream of generate_signals is the
production pipeline. Kelly f is pinned (f_cap == ramp_floor == explore_floor)
and the regime risk scaler is neutral unless a test says otherwise, so sizes
move only with the input under test.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from datetime import timedelta

import numpy as np
import pytest
from tests.conftest import gbm, make_hist, make_state, ts_seq

from quantsys.backtest.simbroker import SimBroker
from quantsys.config.schema import AppConfig
from quantsys.core.market_state import MarketState
from quantsys.core.types import Bar, LegSpec, Position, Signal, Urgency
from quantsys.data.history import BarHistory
from quantsys.engine.decision import DecisionEngine
from quantsys.strategies.base import OnlineEdgeStats, Strategy

E0 = 10_000_000.0
_LABELS = ("calm_trend", "calm_range", "turbulent")


class _Fixed(Strategy):
    """Emits the same signals on every bar."""

    def __init__(self, name: str, signals: list[Signal]) -> None:
        super().__init__(name)
        self.signals = signals

    def generate_signals(self, state: MarketState) -> list[Signal]:
        return list(self.signals)

    def warmup_bars(self) -> int:
        return 0


def _engine(universe: list[dict], signals: list[Signal], name: str = "stub",
            risk_scaler: float = 1.0, **sections: dict) -> DecisionEngine:
    doc: dict = {
        "engine": {"decision_bar_minutes": 5, "cov_window_bars": 60, "cov_halflife_bars": 30.0,
                   "min_order_notional": 1000.0, "stop_cooldown_bars": 3},
        "sizing": {"enforce_cost_gate": False},
        "kelly": {"f_cap": 0.30, "ramp_floor": 0.30, "explore_floor": 0.30},
        "regime": {"labels": {lab: {"risk_scaler": risk_scaler} for lab in _LABELS}},
        "universe": universe,
    }
    for key, block in sections.items():
        doc[key] = {**doc.get(key, {}), **block}
    eng = DecisionEngine(AppConfig.model_validate(doc))
    eng.strategies = [_Fixed(name, signals)]
    k = eng.cfg.kelly
    eng.edge_stats = {name: OnlineEdgeStats(k.edge_halflife_bars, k.prior_obs, k.var_floor)}
    eng.regime_stats = {name: {}}
    return eng


def _hists(closes: dict[str, np.ndarray]) -> dict[str, BarHistory]:
    n = max(len(c) for c in closes.values())
    times = ts_seq(n)
    return {s: make_hist(c, times=times[n - len(c):]) for s, c in closes.items()}


def _state(eng: DecisionEngine, hists: dict[str, BarHistory], equity: float,
           positions: dict[str, Position] | None = None, days: int = 0) -> MarketState:
    t = ts_seq(max(len(h) for h in hists.values()))[-1] + timedelta(days=days)
    return MarketState(ts=t, equity=equity, bars=hists, instruments=eng.instruments,
                       positions=positions or {})


def _notional(d, hists) -> dict[str, float]:
    out: dict[str, float] = {}
    for t in d.targets:
        out[t.symbol] = out.get(t.symbol, 0.0) + t.qty * hists[t.symbol].last_close
    return out


# ------------------------------------------------------------- kill / halt
def test_kill_flatten_publishes_sigma_so_the_fills_pay_impact():
    uni = [{"symbol": "X", "sector": "a", "adv": 2e6},
           {"symbol": "F", "kind": "FUTURE", "lot_size": 50, "sector": "b", "adv": 3e5,
            "margin_rate": 0.15}]
    eng = _engine(uni, [])
    hists = _hists({"X": gbm(120, 100.0, vol=0.004, seed=1),
                    "F": gbm(120, 20_000.0, vol=0.003, seed=2)})
    pos = {"X": Position("X", 400_000, 100.0), "F": Position("F", 1500, 20_000.0)}
    eng.decide(_state(eng, hists, E0, pos))
    d = eng.decide(_state(eng, hists, 0.75 * E0, pos))  # 25% drawdown: hard kill
    assert d.kill_reason == "max_drawdown"
    assert {o.symbol for o in d.orders} == {"X", "F"}
    assert all(o.urgency is Urgency.KILL for o in d.orders)
    for o in d.orders:
        assert d.sigma_daily.get(o.symbol, 0.0) > 0.0, f"{o.symbol} flattened with no sigma"
    prices = {s: h.last_close for s, h in hists.items()}
    paid = SimBroker(1e10, eng.instruments, eng.cost_model)
    paid.execute(d, prices, d.ts)
    blind = SimBroker(1e10, eng.instruments, eng.cost_model)
    blind.execute(dataclasses.replace(d, sigma_daily={}), prices, d.ts)
    assert paid.total_fees > blind.total_fees


def test_halted_bars_do_not_rebook_the_last_unit_book():
    """Price +10 per bar on a frozen engine: only the first halted bar belongs
    to the unit book decided before the halt; later bars must book nothing."""
    eng = _engine([{"symbol": "X", "sector": "a", "adv": 5e6}],
                  [Signal("stub", "X", 1.0, stop_distance=2.0)])
    hists = _hists({"X": np.full(120, 1700.0)})
    times = ts_seq(130)
    E = 1e7
    st = _state(eng, hists, E)
    eng.post_bar(st)
    eng.decide(st)
    q_unit = E * eng.cfg.sizing.base_risk_frac / 2.0  # f=1 unit book: risk / stop
    cost = q_unit * 1700.0 * eng.cost_model_proportional(eng.instruments["X"])
    eng.risk.reconcile({"X": 1}, {"X": 0})
    stats = eng.edge_stats["stub"]
    booked = []
    for k in range(1, 5):
        px = 1700.0 + 10.0 * k
        hists["X"].append(Bar(times[119 + k], px, px, px, px, 1e6))
        s = MarketState(ts=times[119 + k], equity=E, bars=hists, instruments=eng.instruments)
        s0, s1 = stats.s0, stats.s1
        eng.post_bar(s)
        d = eng.decide(s)
        assert d.halted and d.orders == ()
        if stats.s0 != s0:
            booked.append((stats.s1 - stats.lam * s1) * E)
    assert booked == pytest.approx([q_unit * 10.0 - cost], rel=1e-9)


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_equity_halts_the_bar(bad):
    eng = _engine([{"symbol": "X", "sector": "a", "adv": 5e6}],
                  [Signal("stub", "X", 1.0, stop_distance=2.0)])
    hists = _hists({"X": gbm(120, 100.0, vol=0.002, seed=3)})
    pos = {"X": Position("X", 1000, 100.0)}
    d = eng.decide(_state(eng, hists, bad, pos))
    assert d.halted and d.orders == () and d.targets == ()
    assert any(a.rule == "halted" and "equity" in a.detail for a in d.audit)
    assert eng.risk.hwm is None and eng.risk.day_anchor is None
    d2 = eng.decide(_state(eng, hists, E0, pos))
    assert not d2.halted and d2.kill_reason is None
    assert d2.risk_frac_eff == pytest.approx(eng.cfg.sizing.base_risk_frac)
    assert eng.risk.hwm == E0


# ------------------------------------------------------- throttle ordering
def test_drawdown_shrinks_the_final_book_when_vol_targeting_is_interior():
    """The vol targeter scales toward its target, so a throttle applied before
    it is undone whenever the scaler sits between its clips."""
    syms = ["A", "B", "C", "D"]
    uni = [{"symbol": s, "sector": s.lower(), "adv": 5e7, "margin_rate": 0.25} for s in syms]
    hists = _hists({s: gbm(120, 100.0, vol=0.003, seed=10 + i) for i, s in enumerate(syms)})
    sigs = [Signal("stub", s, 1.0, stop_distance=0.25) for s in syms]
    gross, scaler, audits = {}, {}, {}
    for dd in (0.0, 0.045, 0.09):
        eng = _engine(uni, sigs)
        eng.decide(_state(eng, hists, E0))
        eq = E0 * (1.0 - dd)
        d = eng.decide(_state(eng, hists, eq, days=1))  # next session: no day-loss kill
        assert d.kill_reason is None
        gross[dd] = sum(abs(v) for v in _notional(d, hists).values()) / eq
        scaler[dd] = d.vol_scaler
        audits[dd] = {a.rule for a in d.audit}
    assert 0.25 < scaler[0.0] < 2.0, "precondition: the vol scaler must be interior"
    for dd in (0.045, 0.09):
        assert gross[dd] == pytest.approx(gross[0.0] * (1.0 - dd / 0.18), rel=0.01)
        assert "drawdown_throttle" in audits[dd]
    assert "drawdown_throttle" not in audits[0.0]


def test_symbol_without_covariance_history_does_not_scale_the_book_up():
    uni = [{"symbol": "X", "sector": "a", "adv": 5e7}, {"symbol": "Y", "sector": "b", "adv": 5e7}]
    # X has a full, quiet history; Y has fewer than cov_window + 1 bars
    hists = _hists({"X": gbm(120, 100.0, vol=0.0005, seed=4), "Y": gbm(40, 100.0, vol=0.02, seed=5)})
    eng = _engine(uni, [Signal("stub", "X", 1.0, stop_distance=2.0),
                        Signal("stub", "Y", 1.0, stop_distance=2.0)])
    d = eng.decide(_state(eng, hists, E0))
    assert d.vol_scaler <= 1.0
    assert any(a.stage == "vol_target" and "Y" in a.detail and a.rule == "missing_cov"
               for a in d.audit)


# ------------------------------------------------------------ order flow
def test_position_above_the_instrument_cap_is_cut_back_inside_the_band():
    """Held 29.9% of equity, capped target 25%: the 4.9% cut is inside the T4
    band (0.20 * target) but must go out, the held book breaches the cap."""
    eng = _engine([{"symbol": "X", "sector": "a", "adv": 5e8, "margin_rate": 0.2}],
                  [Signal("stub", "X", 1.0, stop_distance=0.25)])
    hists = _hists({"X": np.full(40, 100.0)})
    held = {"X": Position("X", 29_900, 100.0)}
    d = eng.decide(_state(eng, hists, E0, held))
    assert d.tier_name == "T4"
    assert [(t.symbol, t.qty) for t in d.targets] == [("X", 25_000)]
    assert [(o.symbol, o.qty_delta) for o in d.orders] == [("X", -4_900)]


def test_throttle_driven_reduction_is_not_blocked_by_the_band():
    eng = _engine([{"symbol": "X", "sector": "a", "adv": 5e8, "margin_rate": 0.2}],
                  [Signal("stub", "X", 1.0, stop_distance=2.0)])
    hists = _hists({"X": np.full(40, 100.0)})
    d1 = eng.decide(_state(eng, hists, E0))
    held = {t.symbol: Position(t.symbol, t.qty, 100.0) for t in d1.targets}
    assert held, "precondition: the first bar must open a position"
    eq = E0 * (1.0 - 0.018)  # dd 1.8% -> throttle 0.9, next session
    d2 = eng.decide(_state(eng, hists, eq, held, days=1))
    tgt = {t.symbol: t.qty for t in d2.targets}
    cut = held["X"].qty - tgt["X"]
    assert 0 < cut < 0.20 * tgt["X"], "precondition: the cut is inside the band"
    assert [(o.symbol, o.qty_delta) for o in d2.orders] == [("X", -cut)]


def test_a_steady_throttle_keeps_the_band_on_reductions():
    """Only a throttle that fell this bar exempts reductions from the band.
    Exempting every reduction for as long as drawdown was above zero, while
    increases still met the band, ratcheted the book down on noise."""
    eng = _engine([{"symbol": "X", "sector": "a", "adv": 5e8, "margin_rate": 0.2}],
                  [Signal("stub", "X", 1.0, stop_distance=2.0)])
    hists = _hists({"X": np.full(40, 100.0)})
    eng.decide(_state(eng, hists, E0))
    eq = E0 * (1.0 - 0.018)                             # throttle 0.9
    d1 = eng.decide(_state(eng, hists, eq, days=1))
    tgt = {t.symbol: t.qty for t in d1.targets}["X"]
    held = {"X": Position("X", int(tgt * 1.05), 100.0)}  # 5% over: inside the band
    d2 = eng.decide(_state(eng, hists, eq, held, days=1))  # same drawdown, same throttle
    assert {t.symbol: t.qty for t in d2.targets}["X"] == tgt
    assert d2.orders == (), "a reduction inside the band went out with the throttle unchanged"


def test_final_targets_respect_the_cap_after_a_netting_group_is_dropped():
    """Single long X (36% of E) nets against a pair short X (-20%): the net
    passes the 25% cap, then the pair's futures leg rounds to zero lots and the
    pair is dropped, leaving the single leg above the cap."""
    uni = [{"symbol": "X", "sector": "a", "adv": 5e8},
           {"symbol": "F", "kind": "FUTURE", "lot_size": 500, "sector": "b", "adv": 5e8,
            "margin_rate": 0.15}]
    sigs = [Signal("stub", "X", 1.0, stop_distance=0.25),
            Signal("stub", "X", -1.0, stop_distance=0.45,
                   legs=(LegSpec("X", 1.0), LegSpec("F", -0.075)), tag="X|F")]
    E = 1_000_000.0
    eng = _engine(uni, sigs)
    hists = _hists({"X": np.full(40, 100.0), "F": np.full(40, 50.0)})
    d = eng.decide(_state(eng, hists, E))
    nn = _notional(d, hists)
    assert abs(nn.get("X", 0.0)) <= 0.25 * E * (1 + 1e-9)


# ------------------------------------------------------- min-lot promotion
_NIFTY = {"symbol": "NIFTY-FUT", "kind": "FUTURE", "lot_size": 65, "sector": "index",
          "adv": 250_000, "margin_rate": 0.125}
_PROMO = {"min_lot_promotion": True, "promotion_max_risk_frac": 0.005}


def test_min_lot_promotion_cannot_lift_a_capped_position_past_the_cap():
    E = 3_000_000.0  # one lot at 25,000 is 54% of equity; the cap is 25%
    eng = _engine([_NIFTY], [Signal("stub", "NIFTY-FUT", 1.0, stop_distance=100.0)],
                  sizing=_PROMO)
    hists = _hists({"NIFTY-FUT": np.full(40, 25_000.0)})
    d = eng.decide(_state(eng, hists, E))
    for sym, v in _notional(d, hists).items():
        assert abs(v) <= 0.25 * E, f"{sym} at {v / E:.0%} of equity"


def test_min_lot_promotion_ceiling_scales_with_the_drawdown_throttle():
    E = 30_000_000.0  # one lot is 5.4% of equity: inside every cap
    sig = Signal("stub", "NIFTY-FUT", 1.0, stop_distance=100.0)
    hists = _hists({"NIFTY-FUT": np.full(40, 25_000.0)})
    eng = _engine([_NIFTY], [sig], sizing=_PROMO)
    eng.decide(_state(eng, hists, E))
    d = eng.decide(_state(eng, hists, E * (1.0 - 0.178), days=1))  # throttle ~0.011
    assert d.kill_reason is None
    assert d.targets == (), f"promoted at 1% of base risk: {[(t.symbol, t.qty) for t in d.targets]}"


def test_min_lot_promotion_never_turns_a_zero_long_into_a_short():
    E = 30_000_000.0
    eng = _engine([_NIFTY], [Signal("stub", "NIFTY-FUT", 1.0, stop_distance=100.0)],
                  risk_scaler=0.0, sizing=_PROMO)
    hists = _hists({"NIFTY-FUT": np.full(40, 25_000.0)})
    d = eng.decide(_state(eng, hists, E))
    assert all(t.qty > 0 for t in d.targets), [(t.symbol, t.qty) for t in d.targets]
    assert not any(o.qty_delta < 0 for o in d.orders)


# -------------------------------------------------------- sleeve toggles
def _two_sleeve_cfg() -> AppConfig:
    return AppConfig.model_validate({"universe": [{"symbol": "X", "adv": 1e6}]})


def test_disabling_a_sleeve_by_override_does_not_wedge_post_bar():
    cfg = _two_sleeve_cfg()
    eng = DecisionEngine(cfg)
    assert {s.name for s in eng.strategies} >= {"meanrev", "trend"}
    snap = eng.state_dict()
    snap["unit_nets"] = {"meanrev": {"X": 10.0}, "trend": {"X": 5.0}}
    snap["unit_nets_old"] = {"meanrev": {"X": 8.0}, "trend": {"X": 5.0}}
    snap["regime_stats"] = {"meanrev": {"calm_range": {"s0": 1.0, "s1": 0.0, "s2": 0.0}},
                            "trend": {}}
    snap["last_closes"] = {"X": 100.0}
    # what LiveRunner.apply_config_override does: flip the flag, rebuild, restore
    cfg2 = cfg.model_copy(deep=True)
    cfg2.meanrev.enabled = False
    eng2 = DecisionEngine(cfg2)
    eng2.load_state(json.loads(json.dumps(snap)))
    st = make_state({"X": make_hist(np.full(60, 101.0))}, eng2.instruments, equity=1e6)
    for _ in range(2):
        eng2.post_bar(st)
        eng2.decide(st)
    out = eng2.state_dict()
    assert "meanrev" not in out["unit_nets"] and "meanrev" not in out["unit_nets_old"]
    assert "meanrev" not in out["regime_stats"]
    assert eng2.edge_stats["trend"].n_eff > 0


def test_post_bar_skips_an_unknown_sleeve_with_a_warning(caplog):
    eng = DecisionEngine(_two_sleeve_cfg())
    eng._unit_nets = {"ghost": {"X": 10.0}, "trend": {"X": 5.0}}
    eng._last_closes = {"X": 100.0}
    st = make_state({"X": make_hist(np.full(60, 101.0))}, eng.instruments, equity=1e6)
    with caplog.at_level(logging.WARNING, logger="quantsys.engine.decision"):
        eng.post_bar(st)
    assert "ghost" in caplog.text
    assert eng.edge_stats["trend"].n_eff > 0


# ------------------------------------------------------------- warm-up
def test_end_warmup_drops_stop_state_that_no_fill_created():
    uni = [{"symbol": s, "sector": s.lower(), "adv": 5e7} for s in ("X", "Y", "Z")]
    sigs = [Signal("trend", s, 1.0, stop_distance=2.0) for s in ("X", "Y", "Z")]
    eng = _engine(uni, sigs, name="trend")
    n = 60
    times = ts_seq(n + 1)
    closes = {s: np.linspace(100.0, 110.0, n) for s in ("X", "Y", "Z")}
    hists = {s: BarHistory(capacity=n + 16) for s in closes}
    for i in range(n):  # warm-up: targets are decided but never executed
        for s, c in closes.items():
            hists[s].append(Bar(times[i], c[i], c[i], c[i], c[i], 1e6))
        eng.decide(MarketState(ts=times[i], equity=E0, bars=hists, instruments=eng.instruments))
    keys = {e["symbol"] for e in eng.risk.stops.entries.values()}
    assert keys == {"X", "Y", "Z"}, "precondition: warm-up registered stops"
    eng.risk.register_stop_hits({("trend", "Y"): "stop"})

    eng.end_warmup({"X": 0.0, "Z": 250.0})  # Z is really held, X and Y are flat

    left = {e["symbol"] for e in eng.risk.stops.entries.values()}
    assert left == {"Z"}
    assert not eng.risk.in_cooldown("trend", "Y")
    live_px = 104.0
    for s in hists:
        hists[s].append(Bar(times[n], live_px, live_px, live_px, live_px, 1e6))
    eng.decide(MarketState(ts=times[n], equity=E0, bars=hists, instruments=eng.instruments,
                           positions={"Z": Position("Z", 250, 100.0)}))
    x = eng.risk.stops.entries["trend|X"]
    assert x["entry_price"] == live_px and x["best_price"] == live_px
