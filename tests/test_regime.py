import json
import logging
import math

import numpy as np
from tests.conftest import gbm, make_hist, make_inst, make_state

from quantsys.config.schema import RegimeConfig
from quantsys.core.types import InstrumentKind
from quantsys.regime.detector import RegimeDetector
from quantsys.regime.hmm import filtered_probs, fit_hmm


def test_hmm_recovers_two_state_structure():
    rng = np.random.default_rng(42)
    # 2 states: low-vol around +0.5, high-vol around -0.5 (1-D)
    z = (rng.random(600) < 0.5).astype(int)
    # make persistent regimes (sticky chain), not iid switches
    for i in range(1, 600):
        if rng.random() < 0.95:
            z[i] = z[i - 1]
    X = np.where(z == 0, rng.normal(0.5, 0.2, 600), rng.normal(-0.5, 0.6, 600))[:, None]
    params = fit_hmm(X, n_states=2, seed=0, n_restarts=3)
    assert params.converged and not params.degenerate
    means = sorted(params.means[:, 0])
    assert abs(means[0] - (-0.5)) < 0.15
    assert abs(means[1] - 0.5) < 0.15
    # filtered probs are a valid distribution and decisive on clean data
    p = filtered_probs(params, X)
    assert p.sum() == np.float64(1.0) or abs(p.sum() - 1.0) < 1e-9
    assert p.max() > 0.6


def test_hmm_transition_stickiness_learned():
    rng = np.random.default_rng(7)
    z = np.zeros(800, dtype=int)
    for i in range(1, 800):
        z[i] = z[i - 1] if rng.random() < 0.97 else 1 - z[i - 1]
    X = np.where(z == 0, rng.normal(1.0, 0.3, 800), rng.normal(-1.0, 0.3, 800))[:, None]
    params = fit_hmm(X, n_states=2, seed=1, n_restarts=3)
    assert np.diag(params.transmat).min() > 0.85


def _detector_state(closes, cfg):
    nifty = make_inst("NIFTY", kind=InstrumentKind.INDEX)
    bars = {"NIFTY": make_hist(closes)}
    return make_state(bars, {"NIFTY": nifty})


def test_detector_flags_turbulence_and_cuts_risk():
    cfg = RegimeConfig(timeframe_bars=1, train_window=300, refit_every=50,
                       n_restarts=2, n_iter=80)
    det = RegimeDetector(cfg, "NIFTY")
    calm = gbm(400, vol=0.001, seed=3)
    turb = calm[-1] * np.exp(np.cumsum(np.random.default_rng(4).normal(0, 0.012, 80)))
    closes = np.concatenate([calm, turb])
    rs = det.update(_detector_state(closes, cfg))
    assert rs.source in ("hmm", "fallback")
    assert rs.probs["turbulent"] > 0.5
    assert rs.risk_scaler < 0.7  # turbulent regime de-leverages


def test_detector_warmup_is_neutral_and_fallback_safe():
    cfg = RegimeConfig(timeframe_bars=1, train_window=300)
    det = RegimeDetector(cfg, "NIFTY")
    rs = det.update(_detector_state(gbm(20, seed=5), cfg))
    assert rs.source == "warmup"
    assert abs(sum(rs.probs.values()) - 1.0) < 1e-9


def test_detector_state_roundtrip():
    cfg = RegimeConfig(timeframe_bars=1, train_window=200, refit_every=40,
                       n_restarts=2, n_iter=60)
    det = RegimeDetector(cfg, "NIFTY")
    closes = gbm(500, vol=0.004, seed=6)
    st = _detector_state(closes, cfg)
    r1 = det.update(st)
    det2 = RegimeDetector(cfg, "NIFTY")
    det2.load_state(det.state_dict())
    r2 = det2.update(st)
    assert r1.label == r2.label
    assert abs(r1.risk_scaler - r2.risk_scaler) < 0.15



# ------------------------------------------------ non-finite fits and input
def _finite_fit(params) -> bool:
    arrays = (params.startprob, params.transmat, params.means, params.variances,
              params.occupancy)
    return math.isfinite(params.loglik) and all(bool(np.all(np.isfinite(a))) for a in arrays)


def test_hmm_fit_on_data_with_a_nan_row_is_degenerate():
    # NaN compares False, so the occupancy test used to pass a fit whose
    # likelihood, means and occupancy were all NaN.
    X = np.random.default_rng(3).normal(size=(200, 2))
    X[57, 0] = np.nan
    with np.errstate(divide="ignore", invalid="ignore"):
        params = fit_hmm(X, n_states=3, n_iter=20, seed=0, n_restarts=2)
    assert params.degenerate


def test_hmm_never_selects_a_diverged_restart():
    # A third of the observations are exactly 0.0 (a stale price). With the
    # variance floor off, EM collapses a state onto that cluster and goes NaN
    # on the first three restarts of this seed but not on the fourth.
    rng = np.random.default_rng(15)
    X = rng.normal(size=(120, 1))
    X[:40, 0] = 0.0
    rng.shuffle(X)
    with np.errstate(divide="ignore", invalid="ignore"):
        first_only = fit_hmm(X, n_states=3, n_iter=40, seed=15, n_restarts=1, var_floor=0.0)
        params = fit_hmm(X, n_states=3, n_iter=40, seed=15, n_restarts=4, var_floor=0.0)
    assert not _finite_fit(first_only) and first_only.degenerate
    # the diverged first restart used to be kept as "best": no finite
    # likelihood compares greater than NaN
    assert _finite_fit(params) and not params.degenerate


def _assert_usable(rs) -> None:
    assert all(math.isfinite(p) for p in rs.probs.values())
    assert abs(sum(rs.probs.values()) - 1.0) < 1e-9
    assert math.isfinite(rs.risk_scaler)
    assert all(math.isfinite(w) for w in rs.strategy_weights.values())


def test_detector_ignores_a_nan_index_close():
    # One NaN close on a resample bucket end used to reach the fit: the
    # detector published source=hmm with NaN probabilities and a NaN
    # risk_scaler until the next refit.
    cfg = RegimeConfig(timeframe_bars=6, train_window=150, refit_every=125,
                       n_restarts=1, n_iter=30)
    det = RegimeDetector(cfg, "NIFTY")
    closes = gbm(6 * 200, vol=0.002, seed=11)
    closes[-1 - 6 * 40] = np.nan        # resampling is end-aligned: a bucket end
    tail = gbm(6, start=float(closes[-1]), vol=0.002, seed=12)
    # the NaN is on a bucket end on the first and the last of these bars
    for i in range(7):
        rs = det.update(_detector_state(np.concatenate([closes, tail[:i]]), cfg))
        assert rs.source == "hmm"
        _assert_usable(rs)


def _snapshot_of_a_fitted_detector(cfg: RegimeConfig, closes: np.ndarray) -> dict:
    det = RegimeDetector(cfg, "NIFTY")
    det.update(_detector_state(closes, cfg))
    snap = json.loads(json.dumps(det.state_dict()))
    assert snap["params"] is not None
    return snap


def test_detector_does_not_serve_a_restored_nan_model():
    # A snapshot taken after a NaN fit holds NaN parameters flagged healthy.
    cfg = RegimeConfig(timeframe_bars=1, train_window=150, refit_every=50,
                       n_restarts=1, n_iter=30)
    closes = gbm(300, vol=0.004, seed=6)
    snap = _snapshot_of_a_fitted_detector(cfg, closes)
    snap["params"]["means"] = [[float("nan")] * 2 for _ in snap["params"]["means"]]
    det = RegimeDetector(cfg, "NIFTY")
    det.load_state(json.loads(json.dumps(snap)))
    rs = det.update(_detector_state(closes, cfg))      # refit not due
    assert rs.source == "fallback"
    _assert_usable(rs)


def test_detector_falls_back_for_a_bar_whose_state_probabilities_are_non_finite(caplog):
    cfg = RegimeConfig(timeframe_bars=1, train_window=150, refit_every=50,
                       n_restarts=1, n_iter=30)
    closes = gbm(300, vol=0.004, seed=6)
    snap = _snapshot_of_a_fitted_detector(cfg, closes)
    snap["scaler_mu"] = [float("nan"), float("nan")]   # healthy model, NaN filter input
    det = RegimeDetector(cfg, "NIFTY")
    det.load_state(snap)
    with caplog.at_level(logging.WARNING, logger="quantsys.regime.detector"):
        rs = det.update(_detector_state(closes, cfg))
    assert rs.source == "fallback"
    _assert_usable(rs)
    assert any("non-finite" in r.getMessage() for r in caplog.records)


# ------------------------------------------------ label stability on refit
_CALM = ("calm_trend", "calm_range")


def _block_regimes(n: int, seed: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Index closes cycling calm-low (0.3%), turbulent (2.5%) and calm-high
    (0.8%) in 40-bar blocks, all with zero drift. Returns the closes, each
    bar's true state (0 calm-low, 1 calm-high, 2 turbulent) and its age in
    its block. Separated enough that every fit below recovers the same three
    states, so the calm split by |mean| / sd is pure noise."""
    rng = np.random.default_rng(seed)
    period = np.repeat([0, 2, 1], 40)
    reps = n // period.size + 1
    z = np.tile(period, reps)[:n]
    age = np.tile(np.tile(np.arange(40), 3), reps)[:n]
    r = rng.normal(0.0, np.array([0.003, 0.008, 0.025])[z])
    return 100.0 * np.exp(np.cumsum(r)), z, age


def _label_cfg() -> RegimeConfig:
    # refit_every=1: every update() refits, so the test decides where fits land
    return RegimeConfig(timeframe_bars=1, train_window=240, refit_every=1,
                        n_restarts=1, n_iter=60, vol_halflife=5.0)


def _label_at(snapshot: str, cfg: RegimeConfig, closes: np.ndarray) -> str:
    """Label a saved model publishes for the last bar of `closes`, read
    through a restored detector whose refit is not due."""
    frozen = RegimeDetector(cfg.model_copy(update={"refit_every": 10**9}), "NIFTY")
    frozen.load_state(json.loads(snapshot))
    return frozen.update(_detector_state(closes, cfg)).label


def _calm_flip_rate(before: str, after: str, cfg: RegimeConfig, series, lo: int,
                    hi: int) -> tuple[float, int]:
    """Share of probe bars in [lo, hi], each at least 20 bars into a calm
    block, that the two saved models label differently (calm labels only)."""
    closes, z, age = series
    probes = [s for s in range(lo, hi + 1) if z[s - 1] in (0, 1) and age[s - 1] >= 20]
    probes = probes[:: max(1, len(probes) // 10)]
    pairs = [(_label_at(before, cfg, closes[:s]), _label_at(after, cfg, closes[:s]))
             for s in probes]
    calm = [(a, b) for a, b in pairs if a in _CALM and b in _CALM]
    return sum(a != b for a, b in calm) / max(len(calm), 1), len(calm)


def test_refits_do_not_swap_the_calm_labels():
    # The calm_trend / calm_range split was argmax |mean| / sd, which is noise
    # for two zero-drift calm states, and nothing tied a refit's labels to
    # the previous model's. The review measured 73% and 35% of overlapping
    # observations flipping on 2 of 11 refits; on this series two of the
    # three refits relabelled almost every calm bar.
    cfg = _label_cfg()
    ends = [242, 282, 322, 362]              # a refit every 40 bars
    series = _block_regimes(ends[-1], seed=4)
    det = RegimeDetector(cfg, "NIFTY")
    snaps = []
    for e in ends:
        det.update(_detector_state(series[0][:e], cfg))
        snaps.append(json.dumps(det.state_dict()))
    for j in range(1, len(ends)):
        rate, n = _calm_flip_rate(snaps[j - 1], snaps[j], cfg, series,
                                  lo=ends[j] - cfg.train_window + 30, hi=ends[j - 1])
        assert n >= 5
        assert rate <= 0.25, f"refit {j} relabelled {rate:.0%} of calm bars"


def test_restored_detector_keeps_the_calm_labels_across_its_next_refit():
    cfg = _label_cfg()
    series = _block_regimes(282, seed=4)
    live = RegimeDetector(cfg, "NIFTY")
    live.update(_detector_state(series[0][:242], cfg))
    saved = json.dumps(live.state_dict())    # what persistence.save_state writes
    restored = RegimeDetector(cfg, "NIFTY")
    restored.load_state(json.loads(saved))
    for det in (live, restored):
        det.update(_detector_state(series[0], cfg))   # the next refit
    after = json.dumps(restored.state_dict(), sort_keys=True)
    assert after == json.dumps(live.state_dict(), sort_keys=True)
    rate, n = _calm_flip_rate(saved, after, cfg, series, lo=282 - cfg.train_window + 30, hi=242)
    assert n >= 5
    assert rate <= 0.25, f"refit after restore relabelled {rate:.0%} of calm bars"
