"""Engine state across a restart of the live process.

The paper engine is recycled every weekday at 08:50 IST. It used to come back
with a fresh engine: the drawdown kill latch cleared, the down-shock sleeve
forgot its open holds (a 10-session hold lasted one session), and the monthly
factor rebalance fired on every restart. These tests pin the restore path the
live runner now takes after warm-up and daily seeding: save
DecisionEngine.state_dict(), start a new process, re-seed the panels, restore.
A restart must be invisible, except that sessions the process missed while
down still count.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import numpy as np

from conftest import make_hist, make_inst, make_state
from quantsys.config.schema import AppConfig, DownShockConfig, FactorConfig
from quantsys.core.market_state import MarketState
from quantsys.engine.decision import DecisionEngine
from quantsys.strategies.downshock import DownShockStrategy
from quantsys.strategies.factor import FactorStrategy

DAY0 = datetime(2026, 7, 6)          # the shock session
SYMS = ("SHK", "OK")


def _json(d: dict) -> dict:
    """What the runner writes to engine_state and reads back."""
    return json.loads(json.dumps(d))


# --------------------------------------------------------------- downshock
def _ds_cfg(**kw) -> DownShockConfig:
    base = dict(enabled=True, z_threshold=3.5, vol_window=30, volume_window=10,
                volume_ratio_min=2.0, decluster_days=30, hold_days=4,
                max_concurrent=5, atr_n=5, min_history_days=40)
    base.update(kw)
    return DownShockConfig(**base)


def _history() -> dict[datetime, dict[str, tuple[float, float]]]:
    """Daily (close, volume) per symbol: 60 quiet sessions (+-0.3%), an -8%
    shock on 3x volume for SHK on DAY0, then quiet sessions again."""
    out: dict[datetime, dict[str, tuple[float, float]]] = {}
    for i in range(60, 0, -1):
        c = 100.0 * (1 + 0.003 * (1 if i % 2 == 0 else -1))
        out[DAY0 - timedelta(days=i)] = dict.fromkeys(SYMS, (c, 1e6))
    out[DAY0] = {"SHK": (92.0, 3e6), "OK": (100.2, 1e6)}
    for k in range(1, 12):
        out[DAY0 + timedelta(days=k)] = {"SHK": (91.5, 1e6), "OK": (100.0, 1e6)}
    return out


def _seed_rows(hist, sym: str, through: datetime) -> list[tuple]:
    """Broker ONE_DAY candles for `sym` up to and including `through`."""
    return [(d, c, c * 1.005, c * 0.995, c, v)
            for d, row in sorted(hist.items()) if d <= through
            for c, v in [row[sym]]]


def _bar_state(hist, day: datetime) -> MarketState:
    """The first 15-minute decision bar of `day`."""
    insts = {s: make_inst(s) for s in SYMS}
    bars = {s: make_hist([hist[day][s][0]], volume=hist[day][s][1],
                         times=[day.replace(hour=9, minute=15)]) for s in SYMS}
    return make_state(bars, insts, ts=day.replace(hour=9, minute=30))


def _held(strat, hist, day) -> set[str]:
    return {s.symbol for s in strat.generate_signals(_bar_state(hist, day))}


def _restarted_downshock(snapshot: dict, hist, seeded_through: datetime) -> DownShockStrategy:
    """A new process: fresh sleeve, panel re-seeded from the broker, restored."""
    strat = DownShockStrategy(_ds_cfg())
    for s in SYMS:
        strat.seed_daily(s, _seed_rows(hist, s, seeded_through))
    strat.on_seeded()
    strat.restore_state(_json(snapshot))
    return strat


def _downshock_through_day1():
    hist = _history()
    strat = DownShockStrategy(_ds_cfg())
    for s in SYMS:
        strat.seed_daily(s, _seed_rows(hist, s, DAY0 - timedelta(days=1)))
    for k in range(2):  # the shock session, then the entry session
        for bar in (_bar_state(hist, DAY0 + timedelta(days=k)),):
            strat.generate_signals(bar)
    return hist, strat


def test_downshock_hold_survives_a_restart():
    hist, live = _downshock_through_day1()
    assert live._active == {"SHK": 4}, "precondition: entered on the session after the shock"
    snapshot = live.state_dict()

    restarted = _restarted_downshock(snapshot, hist, DAY0 + timedelta(days=1))
    # Fresh without restore, the hold is gone the next morning: the shock row
    # is no longer the newest, so nothing re-detects it.
    fresh = DownShockStrategy(_ds_cfg())
    for s in SYMS:
        fresh.seed_daily(s, _seed_rows(hist, s, DAY0 + timedelta(days=1)))
    assert _held(fresh, hist, DAY0 + timedelta(days=2)) == set()

    for k in range(2, 7):
        day = DAY0 + timedelta(days=k)
        assert _held(restarted, hist, day) == _held(live, hist, day), f"session {k}"
    assert "SHK" not in _held(restarted, hist, DAY0 + timedelta(days=7))


def test_downshock_hold_counts_sessions_missed_while_down():
    hist, live = _downshock_through_day1()
    snapshot = live.state_dict()
    # down for sessions 2 and 3; back on session 4, panel re-seeded through 3
    restarted = _restarted_downshock(snapshot, hist, DAY0 + timedelta(days=3))
    assert _held(restarted, hist, DAY0 + timedelta(days=4)) == {"SHK"}
    assert restarted._active == {"SHK": 1}       # 4 - 3 elapsed sessions
    assert _held(restarted, hist, DAY0 + timedelta(days=5)) == set()


def test_restore_keeps_a_seeded_panel_and_falls_back_to_the_saved_one():
    hist, live = _downshock_through_day1()
    snapshot = live.state_dict()
    seeded = _restarted_downshock(snapshot, hist, DAY0 + timedelta(days=1))
    assert len(seeded._panel["SHK"]) == len(_seed_rows(hist, "SHK", DAY0 + timedelta(days=1)))
    unseeded = DownShockStrategy(_ds_cfg())      # the broker fetch failed at start
    unseeded.restore_state(_json(snapshot))
    assert list(unseeded._panel["SHK"]) == [tuple(r) for r in snapshot["panel"]["SHK"]]


# ----------------------------------------------------------------- factor
def _factor_cfg() -> FactorConfig:
    return FactorConfig(enabled=True, lookback_bars=20, skip_bars=2, vol_lookback=20,
                        rebalance_bars=5, top_k=3, min_universe=12, market_neutral=True,
                        atr_n=5)


def _factor_seeds(end: datetime, flip: bool = False) -> dict[str, list[tuple]]:
    """Twelve names, six trending up and six down; `flip` reverses them."""
    out = {}
    for i in range(12):
        up = (i < 6) != flip
        px = np.linspace(100, 200, 30) if up else np.linspace(200, 100, 30)
        px = px * (1 + 0.001 * i)
        start = end - timedelta(days=29)
        out[f"N{i}"] = [(start + timedelta(days=j), c, c * 1.01, c * 0.99, c, 1e6)
                        for j, c in enumerate(px)]
    return out


def _factor_state(day: datetime) -> MarketState:
    insts = {f"N{i}": make_inst(f"N{i}", adv=1e7) for i in range(12)}
    bars = {s: make_hist([150.0], times=[day.replace(hour=9, minute=15)]) for s in insts}
    return make_state(bars, insts, ts=day.replace(hour=9, minute=30))


def _basket(strat, day) -> set[tuple[str, float]]:
    return {(s.symbol, s.direction) for s in strat.generate_signals(_factor_state(day))}


def test_factor_rebalance_clock_survives_a_restart():
    d0 = datetime(2026, 8, 3)
    live = FactorStrategy(_factor_cfg())
    for s, rows in _factor_seeds(d0 - timedelta(days=1)).items():
        live.seed_daily(s, rows)
    first = _basket(live, d0)                    # rebalances on the first bar
    assert first
    for k in (1, 2):
        _basket(live, d0 + timedelta(days=k))
    snapshot = live.state_dict()

    # Next morning the ranking has turned over. A rebalance now would flip
    # the basket; the schedule says the next one is two sessions away.
    restarted = FactorStrategy(_factor_cfg())
    for s, rows in _factor_seeds(d0 + timedelta(days=2), flip=True).items():
        restarted.seed_daily(s, rows)
    restarted.on_seeded()
    restarted.restore_state(_json(snapshot))
    assert _basket(restarted, d0 + timedelta(days=3)) == first
    assert _basket(restarted, d0 + timedelta(days=4)) == first
    flipped = _basket(restarted, d0 + timedelta(days=5))   # 5 sessions: rebalance
    assert flipped and flipped != first


def test_factor_ranks_only_names_in_the_tier_view():
    """The panel is seeded for every configured equity, but the tier's view
    trades fewer. OUT is the strongest name and has no bar in the view: ranked
    anyway, it took a long slot, its signal was dropped, and the basket went
    out two longs against three shorts."""
    d0 = datetime(2026, 8, 3)
    strat = FactorStrategy(_factor_cfg())
    seeds = _factor_seeds(d0 - timedelta(days=1))
    seeds["OUT"] = [(t, o * (1 + 0.002 * j), h * (1 + 0.002 * j), lo * (1 + 0.002 * j),
                     c * (1 + 0.002 * j), v) for j, (t, o, h, lo, c, v) in enumerate(seeds["N5"])]
    for s, rows in seeds.items():
        strat.seed_daily(s, rows)
    sigs = strat.generate_signals(_factor_state(d0))
    longs = {s.symbol for s in sigs if s.direction > 0}
    shorts = {s.symbol for s in sigs if s.direction < 0}
    assert "OUT" not in longs | shorts
    assert len(longs) == len(shorts) == 3, "basket must stay dollar-neutral"


# ------------------------------------------------------------------ engine
def test_engine_restore_keeps_the_kill_latch_and_drawdown_reference():
    cfg = AppConfig.model_validate({"universe": [{"symbol": "X", "adv": 1e6}]})
    eng = DecisionEngine(cfg)
    t = datetime(2026, 9, 21, 10, 0)
    eng.risk.pre_decide(t, 1_000_000.0)
    assert eng.risk.pre_decide(t, 790_000.0).kill_reason == "max_drawdown"

    restarted = DecisionEngine(cfg)
    restarted.restore_state(_json(eng.state_dict()))
    nxt = t + timedelta(days=1)
    pre = restarted.risk.pre_decide(nxt, 800_000.0)
    assert pre.kill_reason == "max_drawdown", "a manual-re-arm kill must survive a restart"
    assert restarted.risk.hwm == 1_000_000.0



def test_a_rebalance_refused_for_breadth_does_not_restart_the_clock():
    """Warm-up decides on the still-empty panel. A rebalance refused there
    for breadth used to restart the 21-session clock, and a restart that
    restored that clock left the sleeve flat for a whole cycle."""
    d0 = datetime(2026, 8, 3)
    strat = FactorStrategy(_factor_cfg())
    assert _basket(strat, d0 - timedelta(days=1)) == set()      # empty panel
    for s, rows in _factor_seeds(d0 - timedelta(days=1)).items():
        strat.seed_daily(s, rows)
    assert _basket(strat, d0), "the first bar with a full panel must rebalance"


def test_restore_keeps_the_deeper_panel_per_symbol():
    hist, live = _downshock_through_day1()
    snapshot = live.state_dict()
    # seeding failed at this start: warm-up built only a couple of rows
    shallow = DownShockStrategy(_ds_cfg())
    shallow._ingest_daily(_bar_state(hist, DAY0 + timedelta(days=2)))
    shallow._ingest_daily(_bar_state(hist, DAY0 + timedelta(days=3)))
    shallow.restore_state(_json(snapshot))
    assert len(shallow._panel["SHK"]) == len(snapshot["panel"]["SHK"])
    # seeding worked: the fresher seeded panel is kept, not the saved one
    seeded = _restarted_downshock(snapshot, hist, DAY0 + timedelta(days=3))
    assert seeded._panel["SHK"][-1][0] == (DAY0 + timedelta(days=3)).date().isoformat()
