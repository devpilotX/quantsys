import numpy as np
import pytest
from tests.conftest import make_inst

from quantsys.config.schema import KellyConfig, VolTargetConfig
from quantsys.core.types import LegSpec, RegimeState, Signal, bars_per_year
from quantsys.portfolio.allocation import KellyAllocator, VolTargeter
from quantsys.portfolio.book import Component, TargetBook
from quantsys.strategies.base import OnlineEdgeStats

NEUTRAL = RegimeState("calm_range", {"calm_range": 1.0}, 1.0, {}, "test")


def _stats(mean_per_bar: float, sd_per_bar: float, n: int, cfg: KellyConfig) -> OnlineEdgeStats:
    st = OnlineEdgeStats(cfg.edge_halflife_bars, cfg.prior_obs, cfg.var_floor)
    rng = np.random.default_rng(0)
    for r in rng.normal(mean_per_bar, sd_per_bar, n):
        st.update(float(r))
    return st


def test_edge_stats_shrink_young_means_to_zero():
    cfg = KellyConfig(prior_obs=500.0)
    st = OnlineEdgeStats(1000.0, cfg.prior_obs)
    for _ in range(10):
        st.update(0.01)  # 10 great bars
    assert st.raw_mean == pytest.approx(0.01)
    assert st.mean < 0.0002  # shrunk hard: 10 obs vs 500 prior


def test_kelly_formula_clip_and_caps():
    cfg = KellyConfig(kelly_fraction=0.3, f_cap=0.35, gross_f_cap=0.5,
                      ramp_obs=10.0, prior_obs=10.0, edge_halflife_bars=5000.0)
    audits = []
    alloc = KellyAllocator(cfg)
    good = _stats(2e-4, 2e-3, 4000, cfg)   # strong edge -> hits f_cap
    bad = _stats(-3e-4, 2e-3, 4000, cfg)   # established negative edge -> 0
    f = alloc.allocate({"a": good, "b": bad}, NEUTRAL, ["a", "b"], audits)
    assert f["a"] == pytest.approx(min(0.35, 0.5), rel=1e-6) or f["a"] <= 0.35
    assert f["a"] > 0.2
    assert f["b"] == 0.0


def test_kelly_incubation_floor_and_negative_withdrawal():
    cfg = KellyConfig(ramp_floor=0.08, ramp_obs=1000.0, prior_obs=1000.0)
    alloc = KellyAllocator(cfg)
    young = OnlineEdgeStats(cfg.edge_halflife_bars, cfg.prior_obs)
    for _ in range(5):
        young.update(0.0)
    f = alloc.allocate({"s": young}, NEUTRAL, ["s"], [])
    assert f["s"] == pytest.approx(0.08)  # incubation
    losing_young = OnlineEdgeStats(50.0, 10.0)  # fast stats, weak prior
    for _ in range(60):
        losing_young.update(-0.01)
    f2 = alloc.allocate({"s": losing_young}, NEUTRAL, ["s"], [])
    assert f2["s"] == 0.0  # clearly negative evidence withdraws the floor


def test_explore_floor_forces_allocation_despite_negative_edge():
    # paper-only exploration: explore_floor forces a minimum allocation that is
    # NOT withdrawn by clearly-negative evidence. Default 0.0 keeps live and the
    # backtest gate fully honest (asserted in the negative-withdrawal test).
    losing = OnlineEdgeStats(50.0, 10.0)
    for _ in range(60):
        losing.update(-0.01)  # clearly negative edge -> normally withdrawn to 0
    cfg = KellyConfig(ramp_obs=10.0, prior_obs=10.0, edge_halflife_bars=50.0,
                      explore_floor=0.05)
    f = KellyAllocator(cfg).allocate({"s": losing}, NEUTRAL, ["s"], [])
    assert f["s"] == pytest.approx(0.05)  # forced despite negative edge

    cfg_off = KellyConfig(ramp_obs=10.0, prior_obs=10.0, edge_halflife_bars=50.0)
    f_off = KellyAllocator(cfg_off).allocate({"s": losing}, NEUTRAL, ["s"], [])
    assert f_off["s"] == 0.0  # default: still honestly withdrawn


def test_kelly_gross_cap_scales_proportionally():
    cfg = KellyConfig(gross_f_cap=0.4, f_cap=0.35, ramp_obs=1.0, prior_obs=1.0,
                      edge_halflife_bars=5000.0)
    alloc = KellyAllocator(cfg)
    s1 = _stats(3e-4, 2e-3, 4000, cfg)
    s2 = _stats(3e-4, 2e-3, 4000, cfg)
    f = alloc.allocate({"a": s1, "b": s2}, NEUTRAL, ["a", "b"], [])
    assert sum(f.values()) == pytest.approx(0.4, rel=1e-9)
    assert f["a"] == pytest.approx(f["b"])


def test_regime_weights_multiply():
    cfg = KellyConfig(ramp_obs=1.0, prior_obs=1.0, edge_halflife_bars=5000.0)
    alloc = KellyAllocator(cfg)
    st = _stats(2e-4, 2e-3, 4000, cfg)
    reg = RegimeState("turbulent", {"turbulent": 1.0}, 0.35, {"a": 0.5}, "test")
    f_neutral = alloc.allocate({"a": st}, NEUTRAL, ["a"], [])
    f_reg = alloc.allocate({"a": st}, reg, ["a"], [])
    assert f_reg["a"] == pytest.approx(0.5 * f_neutral["a"], rel=1e-9)


def _book_one(symbol: str, qty: float, price: float) -> TargetBook:
    book = TargetBook()
    sig = Signal("s", symbol, 1.0, 1.0, legs=(LegSpec(symbol, 1.0),))
    book.add_group([Component(symbol, qty, "s", "g1", 1.0, price)], sig)
    return book


def test_vol_targeter_exact_math_and_clipping():
    cfg = VolTargetConfig(annual_vol_target=0.13, scaler_min=0.25, scaler_max=2.0)
    vt = VolTargeter(cfg, bar_minutes=5)
    E = 1_000_000.0
    sigma_bar_1d = 0.01 / np.sqrt(bars_per_year(5))  # 1% annual book vol if fully invested
    cov = np.array([[sigma_bar_1d**2]])
    book = _book_one("X", 10_000.0, 100.0)  # notional = E -> w = 1
    s = vt.scaler(book, {"X": 100.0}, {"X": make_inst("X")}, ["X"], cov, E, [])
    assert s == 2.0  # 0.13/0.01 = 13, clipped to scaler_max
    cov_hot = np.array([[(1.0 / np.sqrt(bars_per_year(5))) ** 2]])  # 100% annual vol
    s2 = vt.scaler(book, {"X": 100.0}, {"X": make_inst("X")}, ["X"], cov_hot, E, [])
    assert s2 == pytest.approx(0.25)  # 0.13 wanted, clipped at min
    cov_mid = np.array([[(0.26 / np.sqrt(bars_per_year(5))) ** 2]])
    s3 = vt.scaler(book, {"X": 100.0}, {"X": make_inst("X")}, ["X"], cov_mid, E, [])
    assert s3 == pytest.approx(0.5, rel=1e-6)  # exact: 0.13/0.26


def test_vol_targeter_empty_book_neutral():
    vt = VolTargeter(VolTargetConfig(), 5)
    assert vt.scaler(TargetBook(), {}, {}, [], None, 1e6, []) == 1.0



def test_vol_targeter_non_finite_or_zero_variance_is_neutral_and_audited():
    """A NaN in the covariance made the scaler NaN: every cap comparison was
    then False and finalize raised on int(NaN)."""
    vt = VolTargeter(VolTargetConfig(), 5)
    book = _book_one("X", 1000.0, 100.0)
    for cov in (np.array([[np.nan]]), np.array([[np.inf]]), np.array([[0.0]])):
        audits = []
        s = vt.scaler(book, {"X": 100.0}, {"X": make_inst("X")}, ["X"], cov, 1e6, audits)
        assert s == 1.0
        assert any(a.stage == "vol_target" and a.rule == "degenerate_var" for a in audits)


def _two_name_book() -> TargetBook:
    book = TargetBook()
    for sym, qty in (("X", 1000.0), ("Y", 6000.0)):
        book.add_group([Component(sym, qty, "s", f"s:{sym}", 1.0, 100.0)],
                       Signal("s", sym, 1.0, 1.0))
    return book


def test_vol_targeter_never_scales_up_a_book_with_uncovered_symbols():
    """A symbol with fewer than cov_window + 1 bars is absent from the
    covariance; treating it as riskless took the scaler from 0.661 to 2.0."""
    vt = VolTargeter(VolTargetConfig(), 15)
    insts = {"X": make_inst("X"), "Y": make_inst("Y")}
    prices = {"X": 100.0, "Y": 100.0}
    sd = 0.30 / np.sqrt(bars_per_year(15))
    full = np.array([[sd**2, 0.5 * sd**2], [0.5 * sd**2, sd**2]])
    assert vt.scaler(_two_name_book(), prices, insts, ["X", "Y"], full, 1e6, []) == pytest.approx(
        0.661, abs=1e-3)
    audits = []
    s = vt.scaler(_two_name_book(), prices, insts, ["X"], full[:1, :1], 1e6, audits)
    assert s <= 1.0
    assert any(a.rule == "missing_cov" and "Y" in a.detail for a in audits)
    audits = []
    assert vt.scaler(_two_name_book(), prices, insts, [], None, 1e6, audits) == 1.0
    assert any(a.rule == "missing_cov" and "X" in a.detail and "Y" in a.detail for a in audits)


def test_vol_targeter_may_still_scale_down_a_book_with_uncovered_symbols():
    vt = VolTargeter(VolTargetConfig(), 15)
    insts = {"X": make_inst("X"), "Y": make_inst("Y")}
    hot = np.array([[(2.0 / np.sqrt(bars_per_year(15))) ** 2]])  # 200% annual vol on X
    s = vt.scaler(_two_name_book(), {"X": 100.0, "Y": 100.0}, insts, ["X"], hot, 1e6, [])
    assert s < 1.0


# ------------------------------------------------ Kelly incubation withdrawal
def _loaded(n: float, mean: float, var: float, cfg: KellyConfig) -> OnlineEdgeStats:
    """Stats with exact moments: raw mean `mean`, variance `var`, n_eff `n`."""
    st = OnlineEdgeStats(cfg.edge_halflife_bars, cfg.prior_obs, cfg.var_floor)
    st.load({"s0": n, "s1": n * mean, "s2": n * (var + mean * mean)})
    return st


def test_incubation_floor_survives_one_small_negative_observation():
    cfg = KellyConfig()
    st = OnlineEdgeStats(cfg.edge_halflife_bars, cfg.prior_obs, cfg.var_floor)
    st.update(-1e-5)
    audits = []
    f = KellyAllocator(cfg).allocate({"s": st}, NEUTRAL, ["s"], audits)
    assert f["s"] == pytest.approx(cfg.ramp_floor)
    assert any(a.rule == "incubation_floor" for a in audits)


def test_incubation_floor_withdrawn_only_on_a_clearly_negative_t_stat():
    cfg = KellyConfig()  # ramp_obs 750, prior_obs 750, threshold t <= -2
    alloc = KellyAllocator(cfg)
    n, var = 100.0, 1e-6

    def t_of(st: OnlineEdgeStats) -> float:
        return st.mean / np.sqrt(max(st.var, cfg.var_floor) / st.n_eff)

    weak = _loaded(n, -0.0016, var, cfg)
    clear = _loaded(n, -0.0018, var, cfg)
    assert -2.0 < t_of(weak) < -1.8 and -2.2 < t_of(clear) < -2.0
    assert alloc.allocate({"s": weak}, NEUTRAL, ["s"], [])["s"] == pytest.approx(cfg.ramp_floor)
    assert alloc.allocate({"s": clear}, NEUTRAL, ["s"], [])["s"] == 0.0
    loose = KellyAllocator(KellyConfig(ramp_withdraw_t=3.0))
    assert loose.allocate({"s": clear}, NEUTRAL, ["s"], [])["s"] == pytest.approx(cfg.ramp_floor)


def test_incubation_floor_withdrawal_threshold_must_be_positive():
    with pytest.raises(ValueError):
        KellyConfig(ramp_withdraw_t=0.0)
