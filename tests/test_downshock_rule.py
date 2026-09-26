"""The paper down-shock sleeve against the frozen research rule it claims to be.

docs/FORWARD_STUDY_2.md registers the sleeve as the Pillar-4 research rule,
whose event definition lives in quantsys.research.downshock_tracker
(down_events). The live sleeve had drifted from it: sigma with ddof=0,
de-clustering in calendar days from the last ENTRY instead of sessions from
the last qualifying SHOCK, and only entered events recorded. A NaN volume
also let a shock through on z alone. The cross-check below replays one daily
panel through both and requires the same events.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

from conftest import make_hist, make_inst, make_state
from quantsys.config.schema import DownShockConfig
from quantsys.research import downshock_tracker as research
from quantsys.strategies.downshock import DownShockStrategy


def _weekdays(start: date, n: int) -> list[date]:
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _research_cfg(**kw) -> DownShockConfig:
    """The frozen research constants, no concurrency cap, so the event sets
    compare one to one."""
    base = dict(enabled=True, z_threshold=research.Z_THRESH, vol_window=60,
                volume_window=20, volume_ratio_min=research.V_THRESH,
                decluster_days=research.DECLUSTER, hold_days=research.HOLD,
                max_concurrent=10_000, atr_n=14, min_history_days=80)
    base.update(kw)
    return DownShockConfig(**base)


def _replay(strat: DownShockStrategy, days: list[date], close: pd.DataFrame,
            volume: pd.DataFrame) -> list[tuple[str, date]]:
    """Feed one decision bar per session (the bar carries the day's close and
    volume) and return (symbol, entry session) for every new entry."""
    insts = {s: make_inst(s) for s in close.columns}
    entries: list[tuple[str, date]] = []
    for d in days:
        ts = datetime.combine(d, datetime.min.time()).replace(hour=15, minute=15)
        bars = {s: make_hist([close.at[d, s]], volume=volume.at[d, s],
                             times=[ts]) for s in close.columns}
        before = set(strat._active)
        strat.generate_signals(make_state(bars, insts, ts=ts))
        entries += [(s, d) for s in sorted(set(strat._active) - before)]
    return entries


def _panel(seed: int, n: int = 320, n_sym: int = 5):
    rng = np.random.default_rng(seed)
    days = _weekdays(date(2025, 1, 6), n)
    syms = [f"S{i}" for i in range(n_sym)]
    r = rng.normal(0.0, 0.012, (n, n_sym))
    v = rng.lognormal(13.0, 0.25, (n, n_sym))
    # shocks: big drops, some on heavy volume, some clustered, some without volume
    for i, j, ret, vx in [(120, 0, -0.09, 3.0), (135, 0, -0.10, 3.5),   # 15 sessions apart
                          (180, 0, -0.08, 3.0),                          # 60 after the first
                          (150, 1, -0.09, 1.2),                          # no volume: no event
                          (200, 2, -0.07, 2.5), (228, 2, -0.08, 2.6),    # 28 apart
                          (259, 2, -0.08, 2.4),                          # 31 after the last
                          (240, 3, -0.06, 4.0), (240, 4, -0.07, 4.0)]:
        r[i, j] = ret
        v[i, j] *= vx
    close = pd.DataFrame(100.0 * np.cumprod(1.0 + r, axis=0), index=days, columns=syms)
    volume = pd.DataFrame(v, index=days, columns=syms)
    return days, close, volume


def test_live_events_match_the_frozen_research_rule():
    need = _research_cfg().min_history_days
    for seed in (1, 2, 3):
        days, close, volume = _panel(seed)
        rets = close.pct_change()
        idx = pd.DatetimeIndex([pd.Timestamp(d) for d in days])
        rets.index = volume.index = idx
        expected = sorted(
            (s, days[i + 1]) for s, i in research.down_events(rets, volume, list(rets.columns), idx)
            if i + 1 < len(days) and i + 1 >= need)
        close.index = volume.index = days
        got = sorted(_replay(DownShockStrategy(_research_cfg()), days, close, volume))
        assert got == expected, f"seed {seed}"
        assert len(expected) >= 5, "the panel must exercise the rule"


def _one_name_panel(n: int = 140):
    days = _weekdays(date(2025, 1, 6), n)
    base = np.where(np.arange(n) % 2 == 0, 0.004, -0.004)   # sigma exactly 0.004 (ddof=0)
    return days, base


def test_sigma_uses_ddof_1_like_the_research_rule():
    days, r = _one_name_panel()
    # z = -3.52 with a ddof=0 sigma, -3.46 with ddof=1: not an event at 3.5
    r[100] = -3.52 * 0.004 * (1.0 - 1e-9)
    close = pd.DataFrame({"X": 100.0 * np.cumprod(1.0 + r)}, index=days)
    volume = pd.DataFrame({"X": np.where(np.arange(len(days)) == 100, 5e6, 1e6)}, index=days)
    assert _replay(DownShockStrategy(_research_cfg(vol_window=30)), days, close, volume) == []


def test_nan_volume_does_not_let_a_shock_through_on_z_alone():
    days, r = _one_name_panel()
    r[100] = -0.03                                          # z far past 3.5
    close = pd.DataFrame({"X": 100.0 * np.cumprod(1.0 + r)}, index=days)
    vol = np.full(len(days), 1e6)                           # shock day volume ratio 1.0
    clean = pd.DataFrame({"X": vol}, index=days)
    assert _replay(DownShockStrategy(_research_cfg(vol_window=30)), days, close, clean) == []
    vol_nan = vol.copy()
    vol_nan[95] = np.nan                                    # one bad print in the window
    dirty = pd.DataFrame({"X": vol_nan}, index=days)
    assert _replay(DownShockStrategy(_research_cfg(vol_window=30)), days, close, dirty) == []


def test_a_capped_event_still_starts_a_cluster():
    """Y's shock is capped (one slot, X deeper), yet it is a qualifying event
    and blocks Y's next shock 12 sessions later. Recording only entered events
    let that second shock in."""
    days = _weekdays(date(2025, 1, 6), 140)
    n = len(days)
    base = np.where(np.arange(n) % 2 == 0, 0.004, -0.004)
    rx, ry = base.copy(), base.copy()
    rx[100], ry[100], ry[112] = -0.05, -0.03, -0.03
    close = pd.DataFrame({"X": 100 * np.cumprod(1 + rx), "Y": 100 * np.cumprod(1 + ry)},
                         index=days)
    heavy = np.where(np.isin(np.arange(n), [100, 112]), 5e6, 1e6)
    volume = pd.DataFrame({"X": heavy, "Y": heavy}, index=days)
    strat = DownShockStrategy(_research_cfg(vol_window=30, max_concurrent=1, hold_days=5))
    entries = _replay(strat, days, close, volume)
    assert entries == [("X", days[101])]
