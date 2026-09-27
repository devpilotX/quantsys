from datetime import datetime, timedelta

import pytest
from tests.conftest import gbm, make_hist, make_inst, make_state

from quantsys.config.schema import DrawdownConfig
from quantsys.core.types import TargetPosition
from quantsys.risk.engine import RiskEngine, StopTracker

T0 = datetime(2026, 1, 5, 9, 20)


def _engine(**kw) -> RiskEngine:
    cfg = DrawdownConfig(**kw)
    return RiskEngine(cfg, base_risk_frac=0.006, trailing_strategies={"trend"},
                      stop_cooldown_bars=3)


def test_drawdown_throttle_linear_and_floor():
    eng = _engine(max_drawdown=0.18)
    eng.pre_decide(T0, 100.0)                      # sets HWM=100
    pre = eng.pre_decide(T0 + timedelta(minutes=5), 91.0)  # dd = 9%
    assert pre.throttle == pytest.approx(0.5)
    assert pre.risk_frac_eff == pytest.approx(0.003)
    pre2 = eng.pre_decide(T0 + timedelta(minutes=10), 82.0)  # dd = 18%
    assert pre2.throttle == 0.0
    assert pre2.risk_frac_eff == 0.0


def test_daily_loss_kill_and_auto_rearm_next_session():
    eng = _engine(daily_loss_limit=0.025, auto_rearm_daily=True)
    eng.pre_decide(T0, 100.0)
    pre = eng.pre_decide(T0 + timedelta(minutes=30), 97.4)  # -2.6% on the day
    assert pre.kill_reason == "daily_loss_limit"
    # still killed later the same session even if price recovers
    pre2 = eng.pre_decide(T0 + timedelta(hours=2), 99.5)
    assert pre2.kill_reason == "daily_loss_limit"
    # next session: re-armed, new anchor
    pre3 = eng.pre_decide(T0 + timedelta(days=1), 97.4)
    assert pre3.kill_reason is None


def test_max_drawdown_kill_requires_manual_rearm():
    eng = _engine(kill_drawdown=0.20)
    eng.pre_decide(T0, 100.0)
    pre = eng.pre_decide(T0 + timedelta(days=1), 79.0)  # dd 21%
    assert pre.kill_reason == "max_drawdown"
    pre2 = eng.pre_decide(T0 + timedelta(days=2), 95.0)  # recovery does NOT rearm
    assert pre2.kill_reason == "max_drawdown"
    eng.rearm()
    pre3 = eng.pre_decide(T0 + timedelta(days=2, minutes=5), 95.0)
    assert pre3.kill_reason is None


def test_equity_wipeout_kills():
    eng = _engine()
    assert eng.pre_decide(T0, -5.0).kill_reason == "max_drawdown"


def test_reconciliation_mismatch_halts_until_rearm():
    eng = _engine()
    mism = eng.reconcile({"X": 100}, {"X": 50, "Y": 10})
    assert len(mism) == 2
    assert eng.pre_decide(T0, 100.0).halted_reason is not None
    eng.rearm()
    assert eng.pre_decide(T0 + timedelta(minutes=5), 100.0).halted_reason is None


def _stop_state(symbol: str, price: float):
    bars = {symbol: make_hist(gbm(30, start=price, vol=1e-6, seed=9))}
    return make_state(bars, {symbol: make_inst(symbol)})


def test_hard_stop_long_and_short():
    st = StopTracker(trailing_strategies=set())
    st.refresh([
        TargetPosition("L", 100, "s", "g1", stop_distance=5.0, ref_price=100.0),
        TargetPosition("S", -100, "s", "g2", stop_distance=5.0, ref_price=100.0),
    ])
    assert st.breached(_stop_state("L", 95.5)) == {}
    hits = st.breached(_stop_state("L", 94.9))
    assert ("s", "L") in hits
    hits2 = st.breached(_stop_state("S", 105.2))
    assert ("s", "S") in hits2


def test_trailing_stop_ratchets():
    st = StopTracker(trailing_strategies={"trend"})
    st.refresh([TargetPosition("X", 100, "trend", "g", stop_distance=5.0, ref_price=100.0)])
    st.update_prices(_stop_state("X", 110.0))        # best -> 110, stop -> 105
    assert st.breached(_stop_state("X", 105.5)) == {}
    hits = st.breached(_stop_state("X", 104.9))
    assert ("trend", "X") in hits  # would NOT have hit the static 95 stop


def test_stop_cooldown_lockout():
    # stop_cooldown_bars=3: the three decision bars after the hit bar are
    # blocked and the fourth is free
    eng = _engine()
    eng.pre_decide(T0, 100.0)  # the hit bar
    eng.register_stop_hits({("trend", "X"): "stop"})
    assert eng.in_cooldown("trend", "X")
    for i in range(1, 4):
        eng.pre_decide(T0 + timedelta(minutes=5 * i), 100.0)
        assert eng.in_cooldown("trend", "X"), f"bar {i} after the hit was not blocked"
    eng.pre_decide(T0 + timedelta(minutes=20), 100.0)
    assert not eng.in_cooldown("trend", "X")


def test_stop_cooldown_blocks_exactly_n_decision_bars():
    eng = RiskEngine(DrawdownConfig(), 0.006, set(), stop_cooldown_bars=12)
    eng.pre_decide(T0, 1e6)
    eng.register_stop_hits({("trend", "X"): "stop"})
    blocked = 0
    for k in range(1, 30):
        eng.pre_decide(T0 + timedelta(minutes=15 * k), 1e6)
        if not eng.in_cooldown("trend", "X"):
            break
        blocked += 1
    assert blocked == 12


# ------------------------------------------------------ non-finite equity
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), None])
def test_unreadable_equity_halts_the_bar_and_never_becomes_a_reference(bad):
    eng = _engine()
    pre = eng.pre_decide(T0, bad)
    assert pre.halted_reason is not None and "equity" in pre.halted_reason
    assert pre.risk_frac_eff == 0.0
    assert eng.hwm is None and eng.day_anchor is None
    eng.pre_decide(T0 + timedelta(minutes=5), 100.0)
    assert eng.hwm == 100.0 and eng.day_anchor == 100.0
    assert eng.pre_decide(T0 + timedelta(minutes=10), 97.0).kill_reason == "daily_loss_limit"


def test_nan_first_equity_does_not_disable_the_drawdown_kill():
    eng = _engine(kill_drawdown=0.20)
    eng.pre_decide(T0, float("nan"))
    eng.pre_decide(T0 + timedelta(minutes=5), 100.0)
    pre = eng.pre_decide(T0 + timedelta(days=3), 79.0)
    assert pre.drawdown == pytest.approx(0.21)
    assert pre.kill_reason == "max_drawdown"


def test_nan_equity_at_session_open_does_not_disable_the_daily_kill():
    eng = _engine(daily_loss_limit=0.025)
    eng.pre_decide(T0, 100.0)
    eng.pre_decide(T0 + timedelta(days=1), float("nan"))       # session's first bar
    eng.pre_decide(T0 + timedelta(days=1, minutes=5), 90.0)    # first readable: the anchor
    pre = eng.pre_decide(T0 + timedelta(days=1, minutes=10), 87.5)
    assert pre.day_pnl == pytest.approx(-2.5)
    assert pre.kill_reason == "daily_loss_limit"


def test_persisted_nan_references_are_discarded_on_load():
    eng = _engine(kill_drawdown=0.20)
    eng.load_state({"hwm": float("nan"), "day_anchor": float("nan"), "day_date": "2026-01-05"})
    eng.pre_decide(T0, 100.0)
    assert eng.hwm == 100.0 and eng.day_anchor == 100.0
    assert eng.pre_decide(T0 + timedelta(days=1), 79.0).kill_reason == "max_drawdown"


# ------------------------------------------------------- re-arm / re-base
def test_rearm_after_a_drawdown_kill_rebases_to_the_next_equity():
    eng = _engine(kill_drawdown=0.20)
    eng.pre_decide(T0, 100.0)
    assert eng.pre_decide(T0 + timedelta(days=1), 79.5).kill_reason == "max_drawdown"
    eng.rearm()
    pre = eng.pre_decide(T0 + timedelta(days=1, minutes=5), 79.5)  # flat book, same equity
    assert pre.kill_reason is None
    assert pre.drawdown == 0.0 and pre.throttle == 1.0
    assert pre.risk_frac_eff == pytest.approx(0.006)


def test_rearm_between_the_throttle_floor_and_the_kill_restores_risk():
    eng = _engine(max_drawdown=0.18, kill_drawdown=0.20)
    eng.pre_decide(T0, 100.0)
    eng.pre_decide(T0 + timedelta(days=1), 79.0)
    assert eng.pre_decide(T0 + timedelta(days=2), 81.5).kill_reason == "max_drawdown"
    eng.rearm()
    pre = eng.pre_decide(T0 + timedelta(days=2, minutes=5), 81.5)
    assert pre.kill_reason is None
    assert pre.risk_frac_eff == pytest.approx(0.006)


def test_rearm_to_an_explicit_baseline():
    eng = _engine(max_drawdown=0.18, kill_drawdown=0.20)
    eng.pre_decide(T0, 100.0)
    eng.pre_decide(T0 + timedelta(days=1), 79.0)
    eng.rearm(baseline=85.0)
    pre = eng.pre_decide(T0 + timedelta(days=1, minutes=5), 79.0)
    assert pre.kill_reason is None
    assert pre.drawdown == pytest.approx(1.0 - 79.0 / 85.0)


@pytest.mark.parametrize("bad", [0.0, -1.0, float("nan"), float("inf")])
def test_rearm_rejects_an_unusable_baseline(bad):
    eng = _engine()
    eng.pre_decide(T0, 100.0)
    eng.pre_decide(T0 + timedelta(days=1), 79.0)
    with pytest.raises(ValueError):
        eng.rearm(baseline=bad)
    assert eng.dd_killed and eng.hwm == 100.0


def test_rearm_clears_a_day_kill_when_auto_rearm_is_off():
    eng = _engine(auto_rearm_daily=False)
    eng.pre_decide(T0, 100.0)
    assert eng.pre_decide(T0 + timedelta(minutes=15), 97.0).kill_reason == "daily_loss_limit"
    assert eng.pre_decide(T0 + timedelta(days=7), 97.0).kill_reason == "daily_loss_limit"
    eng.rearm()
    assert eng.pre_decide(T0 + timedelta(days=7, minutes=15), 97.0).kill_reason is None


def test_clear_halt_leaves_kill_and_drawdown_state_alone():
    eng = _engine(kill_drawdown=0.20)
    eng.pre_decide(T0, 100.0)
    eng.pre_decide(T0 + timedelta(days=1), 79.0)
    eng.reconcile({"X": 1}, {})
    eng.clear_halt()
    pre = eng.pre_decide(T0 + timedelta(days=1, minutes=5), 79.0)
    assert pre.halted_reason is None
    assert pre.kill_reason == "max_drawdown" and eng.hwm == 100.0


def test_rebase_equity_books_a_capital_change_as_neither_gain_nor_loss():
    eng = _engine()
    eng.pre_decide(T0, 100.0)
    eng.rebase_equity(100.0, 80.0)  # operator cuts deployable capital by 20%
    pre = eng.pre_decide(T0 + timedelta(minutes=5), 80.0)
    assert pre.kill_reason is None
    assert pre.drawdown == pytest.approx(0.0) and pre.day_pnl == pytest.approx(0.0)
    assert pre.throttle == 1.0
    pre2 = eng.pre_decide(T0 + timedelta(minutes=10), 79.0)  # a real loss still counts
    assert pre2.day_pnl == pytest.approx(-1.0)
    assert pre2.drawdown == pytest.approx(0.0125)
    eng.rebase_equity(79.0, 158.0)  # a deposit is not a gain either
    pre3 = eng.pre_decide(T0 + timedelta(minutes=15), 158.0)
    assert pre3.drawdown == pytest.approx(0.0125) and pre3.day_pnl == pytest.approx(-2.0)
    assert pre3.kill_reason is None


@pytest.mark.parametrize(("old", "new"), [(0.0, 80.0), (100.0, 0.0), (-1.0, 80.0),
                                          (float("nan"), 80.0), (100.0, float("inf"))])
def test_rebase_equity_rejects_unusable_inputs(old, new):
    eng = _engine()
    eng.pre_decide(T0, 100.0)
    with pytest.raises(ValueError):
        eng.rebase_equity(old, new)
    assert eng.hwm == 100.0 and eng.day_anchor == 100.0


def test_state_roundtrip():
    eng = _engine()
    eng.pre_decide(T0, 100.0)
    eng.pre_decide(T0 + timedelta(minutes=5), 90.0)
    eng.register_stop_hits({("s", "X"): "stop"})
    d = eng.state_dict()
    eng2 = _engine()
    eng2.load_state(d)
    assert eng2.hwm == 100.0
    assert eng2.in_cooldown("s", "X")
