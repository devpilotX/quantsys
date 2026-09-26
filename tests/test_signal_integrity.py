"""Signal-layer regression tests: the pairs rearm latch across a rescan, the
reversal selection when k is 0, the config bounds that keep k and the regime
risk scaler sane, and the options sleeve's realized-vol clock.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.conftest import cointegrated_pair, make_hist, make_inst, make_state

from quantsys.config.schema import (
    FactorConfig,
    MeanRevConfig,
    RegimeLabelConfig,
    ReversalConfig,
    VolOptionsConfig,
)
from quantsys.core.market_state import MarketState
from quantsys.core.types import InstrumentKind, bars_per_year
from quantsys.options.chain import SyntheticChainProvider
from quantsys.strategies.meanrev import MeanRevStrategy
from quantsys.strategies.reversal import ReversalStrategy
from quantsys.strategies.voloptions import VolOptionsStrategy


# ------------------------------------------------------- meanrev rearm latch
def _pair_state(a, b):
    return make_state({"A": make_hist(a), "B": make_hist(b)},
                      {"A": make_inst("A", sector="fin"), "B": make_inst("B", sector="fin")})


@pytest.mark.parametrize("stop_z", [4.0, 2.6], ids=["z_stop", "time_stop"])
def test_meanrev_rescan_keeps_the_rearm_latch(stop_z):
    # A z-stop or a time stop latches the pair: no re-entry until |z| falls
    # back below z_entry. The rescan rebuilt flat pairs carrying only the
    # cooldown, so the rescan bar re-entered at once with |z| still inside
    # [z_entry, z_stop).
    cfg = MeanRevConfig(timeframe_bars=1, lookback=150, rescan_every=24, min_half_life=3.0,
                        max_half_life=80.0, max_pairs=2, z_entry=2.0, z_exit=0.5,
                        z_stop=3.5, cooldown_bars=3)
    a, b = cointegrated_pair(220, beta=1.0, kappa=0.12, seed=7)
    strat = MeanRevStrategy(cfg)
    strat.generate_signals(_pair_state(a, b))            # call 1: scan
    [key] = strat._pairs

    def at_z(z: float):
        """Pair state with A's last close put at spread z under the pair's
        current parameters."""
        p = strat._pairs[key]
        a2 = a.copy()
        a2[-1] = math.exp(p["theta"] + z * p["sigma_eq"] + p["beta"] * math.log(b[-1]))
        return _pair_state(a2, b)

    counts = [len(strat.generate_signals(at_z(2.6)))]    # call 2: enter
    counts.append(len(strat.generate_signals(at_z(stop_z))))
    counts += [len(strat.generate_signals(at_z(2.6))) for _ in range(21)]   # calls 4-24
    assert counts[0] == 1
    assert counts[-5:] == [0] * 5                        # stopped out, cooled down, latched
    assert strat.generate_signals(at_z(2.6)) == []       # call 25: the rescan bar
    assert key in strat._pairs                           # the rescan did refit the pair
    # the latch still releases the normal way
    assert strat.generate_signals(at_z(0.0)) == []
    assert len(strat.generate_signals(at_z(2.6))) == 1


# ------------------------------------------------------ reversal selection
def _reversal(n_names: int, top_k: int) -> tuple[ReversalStrategy, MarketState]:
    """Seeded reversal sleeve over n names with distinct 5-day returns, plus
    the next session's state."""
    cfg = ReversalConfig(enabled=True, lookback_days=5, rebalance_days=5, top_k=top_k,
                         min_universe=1, market_neutral=True, atr_n=5)
    strat = ReversalStrategy(cfg)
    end = datetime(2026, 7, 3)
    day = end + timedelta(days=1)
    insts, bars = {}, {}
    for i in range(n_names):
        sym = f"S{i}"
        px = np.linspace(100.0, 100.0 + i - n_names / 2, 20)
        strat.seed_daily(sym, [(end - timedelta(days=19 - j), p, p * 1.01, p * 0.99, p, 1e6)
                               for j, p in enumerate(px)])
        insts[sym] = make_inst(sym)
        bars[sym] = make_hist([px[-1]], times=[day.replace(hour=9, minute=15)])
    return strat, make_state(bars, insts, ts=day.replace(hour=10))


def test_reversal_one_name_cross_section_trades_nothing():
    # k = min(top_k, 1 // 2) = 0 and ranked[-0:] is the whole list, so the
    # single name was shorted.
    strat, state = _reversal(1, top_k=8)
    assert strat.generate_signals(state) == []


@settings(max_examples=60, deadline=None, database=None)
@given(n_names=st.integers(min_value=1, max_value=12), top_k=st.integers(min_value=1, max_value=8))
def test_reversal_legs_are_balanced_and_disjoint(n_names, top_k):
    strat, state = _reversal(n_names, top_k)
    sigs = strat.generate_signals(state)
    longs = {s.symbol for s in sigs if s.direction > 0}
    shorts = {s.symbol for s in sigs if s.direction < 0}
    k = min(top_k, n_names // 2)
    assert len(longs) == len(shorts) == k
    assert not longs & shorts


@pytest.mark.parametrize("model", [ReversalConfig, FactorConfig])
def test_cross_sectional_top_k_must_be_positive(model):
    with pytest.raises(ValidationError):
        model(top_k=0)
    assert model(top_k=1).top_k == 1


def test_regime_risk_scaler_cannot_be_negative():
    with pytest.raises(ValidationError):
        RegimeLabelConfig(risk_scaler=-0.1)
    assert RegimeLabelConfig(risk_scaler=0.0).risk_scaler == 0.0   # a flat regime stays legal


# ------------------------------------------------- options realized-vol clock
def _session_times(n: int, bar_minutes: int) -> list[datetime]:
    """Decision-bar timestamps over consecutive NSE sessions (09:15 start)."""
    per_session = 375 // bar_minutes
    t0 = datetime(2026, 6, 1, 9, 15)
    return [t0 + timedelta(days=i // per_session, minutes=bar_minutes * (i % per_session))
            for i in range(n)]


def _vol_setup(times: list[datetime], iv_over_rv15: float, extra: dict | None = None):
    """Options sleeve plus a state whose chain prices ATM IV at a multiple of
    the underlying's realized vol annualised on a 15-minute clock."""
    cfg = VolOptionsConfig(enabled=True)
    closes = 24000.0 * np.exp(np.cumsum(np.random.default_rng(3).normal(0.0, 0.001, len(times))))
    rets = np.diff(np.log(closes[-(cfg.rv_window + 1):]))
    rv15 = float(np.std(rets, ddof=1)) * math.sqrt(bars_per_year(15))
    chain = SyntheticChainProvider(iv=iv_over_rv15 * rv15, days_to_expiry=7).chain(
        "NIFTY", float(closes[-1]), times[-1])
    state = make_state({"NIFTY": make_hist(closes, times=times)},
                       {"NIFTY": make_inst("NIFTY", kind=InstrumentKind.INDEX)}, ts=times[-1])
    state.extra.update({"option_chains": {"NIFTY": chain}, **(extra or {})})
    return VolOptionsStrategy(cfg), state


def test_voloptions_reads_a_15_minute_clock_from_the_bars():
    # Nothing sets extra["bar_minutes"]; the default of 5 overstated realized
    # vol by sqrt(3) on 15-minute bars, so IV at 1.5x the true RV read as
    # 0.87x (neither rich nor cheap) and the sleeve stayed flat.
    strat, state = _vol_setup(_session_times(200, 15), iv_over_rv15=1.5)
    sigs = strat.generate_signals(state)
    assert len(sigs) == 1 and "credit" in sigs[0].tag


def test_voloptions_bar_minutes_in_extra_takes_precedence():
    strat, state = _vol_setup(_session_times(200, 15), iv_over_rv15=1.5, extra={"bar_minutes": 5})
    assert strat.generate_signals(state) == []           # 1.5 / sqrt(3): not rich


def test_voloptions_without_a_usable_clock_emits_nothing_and_says_so(caplog):
    # One bar per session: no intra-session gap to read the interval from.
    daily = [datetime(2026, 6, 1, 9, 15) + timedelta(days=i) for i in range(200)]
    strat, state = _vol_setup(daily, iv_over_rv15=3.0)  # rich under any guessed clock
    with caplog.at_level(logging.WARNING, logger="quantsys.strategies.voloptions"):
        assert strat.generate_signals(state) == []
    assert any("bar_minutes" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("bad", [0, -15, float("nan")])
def test_voloptions_rejects_a_nonsensical_bar_minutes(bad):
    strat, state = _vol_setup(_session_times(200, 15), iv_over_rv15=1.5, extra={"bar_minutes": bad})
    with pytest.raises(ValueError, match="bar_minutes"):
        strat.generate_signals(state)
