"""LiveRunner start-up: what survives the daily 08:50 recycle, and what the
warm-up must and must not leave behind.

Paper runner on a mock Angel transport, real engine, real PaperBroker,
SQLite test DB. Every test clears the engine_state row it may write.
"""

from __future__ import annotations

import dataclasses
import threading
from datetime import datetime, time, timedelta

import numpy as np
import pytest
from qsdash.db import now_ist
from qsdash.models import EngineState, MarketBar, RuntimeConfig
from tests.conftest import REPO_ROOT

from quantsys.core.types import Position

CFG = str(REPO_ROOT / "config" / "base.yaml")


class _MockTransport:
    def generateSession(self, c, m, t):
        return {"status": True, "data": {"refreshToken": "rt"}}

    def getfeedToken(self):
        return "ft"

    def rmsLimit(self):
        return {"status": True, "data": {"availablecash": "1000000"}}

    def position(self):
        return {"status": True, "data": []}

    def orderBook(self):
        return {"status": True, "data": []}


def _runner():
    """A paper LiveRunner built the way test_single_decide builds one."""
    from qsdash.bridge.commands import CommandConsumer
    from qsdash.bridge.live import LiveRunner
    from qsdash.bridge.paper import PaperBroker
    from qsdash.bridge.recorder import Recorder
    from qsdash.bus import make_sync_publisher
    from qsdash.db import SessionLocal

    from quantsys.config import load_config
    from quantsys.data.history import BarHistory
    from quantsys.engine.decision import DecisionEngine
    from quantsys.execution.angelone import AngelOneBroker
    from quantsys.execution.marketdata import BarAggregator

    adapter = AngelOneBroker(api_key="k", client_code="c", mpin="1",
                             totp_secret="JBSWY3DPEHPK3PXP", transport=_MockTransport(),
                             instrument_master=[])
    adapter.connect()
    r = LiveRunner.__new__(LiveRunner)
    r.cfg = load_config(CFG)
    r.mode = "paper"
    r.publisher = make_sync_publisher(SessionLocal)
    r.broker_adapter = adapter
    r.instruments = r._merge_instruments(adapter.instruments())
    r.engine = DecisionEngine(r.cfg, instruments=r.instruments)
    r._all_strategies = list(r.engine.strategies)
    r._disabled = set()
    r.recorder = Recorder("paper", r.publisher)
    r.broker = PaperBroker("paper", 1_000_000.0, r.instruments, r.engine.cost_model,
                           r.publisher)
    r.commands = CommandConsumer(r, r.publisher)
    r.histories = {s: BarHistory() for s in r.instruments}
    r._last_prices = {}
    r.deployable_cap_frac = None
    r.deployable_cap_abs = None
    r._pending_bars = {}
    r._bar_accum = {}
    r._last_step_bucket = None
    r._lock = threading.Lock()
    r.feed = None
    r._last_kill = None
    r._last_halted = False
    r._feed_stale = False
    r._started_monotonic = 0.0
    r.paused = False
    r.aggregator = BarAggregator(r.cfg.engine.decision_bar_minutes, r._on_completed_bar)
    return r


@pytest.fixture(autouse=True)
def _clean(db):
    def wipe():
        db.query(EngineState).delete()
        db.query(MarketBar).filter(MarketBar.symbol == "HDFCBANK").delete()
        db.query(RuntimeConfig).filter(RuntimeConfig.key == "paper_broker_state").delete()
        db.commit()

    wipe()
    yield
    wipe()


def _session_bars(start: datetime, end: datetime, minutes: int = 15):
    """Decision-bar stamps of every NSE session bar in [start, end]."""
    out = []
    d = start.date()
    while d <= end.date():
        if d.weekday() < 5:
            t = datetime.combine(d, time(9, 15))
            while t.time() < time(15, 30):
                if start <= t <= end:
                    out.append(t)
                t += timedelta(minutes=minutes)
        d += timedelta(days=1)
    return out


# ---------------------------------------------------- saved engine state
def test_restart_restores_the_kill_latch_and_the_drawdown_reference(db):
    ts = datetime(2026, 9, 21, 11, 0)
    first = _runner()
    first.engine.risk.pre_decide(ts, 1_000_000.0)
    assert first.engine.risk.pre_decide(ts, 790_000.0).kill_reason == "max_drawdown"
    first._persist_engine_state(ts)
    row = db.get(EngineState, "paper")
    assert row is not None and row.bar_ts == ts and row.version == 1

    second = _runner()                       # the 08:50 recycle: a new process
    assert second.engine.risk.dd_killed is False
    second._resume_engine()
    assert second.engine.risk.dd_killed is True
    assert second.engine.risk.hwm == 1_000_000.0


def test_an_unrestorable_save_is_kept_and_never_overwritten(db):
    db.add(EngineState(mode="paper", saved_at=now_ist(), config_hash="x", version=1,
                       state={"risk": {"cooldowns": "not a mapping"}}))
    db.commit()
    r = _runner()
    assert r._restore_engine_state() is False
    r.engine.risk.pre_decide(datetime(2026, 9, 21, 11, 0), 1_000_000.0)
    r._persist_engine_state(datetime(2026, 9, 21, 11, 0))
    db.expire_all()
    assert db.get(EngineState, "paper").state == {"risk": {"cooldowns": "not a mapping"}}


def test_a_save_from_a_newer_build_is_not_loaded(db):
    db.add(EngineState(mode="paper", saved_at=now_ist(), config_hash="x", version=99,
                       state={"risk": {"dd_killed": True}}))
    db.commit()
    r = _runner()
    assert r._restore_engine_state() is False
    assert r.engine.risk.dd_killed is False


# ------------------------------------------------------------- warm-up
def _fake_15min(symbols: dict[str, callable]):
    """historical_candles stand-in honouring the requested window: only
    bars inside [start, end] exist, as with the broker."""
    def fetch(sym, interval, start, end):
        if interval != "FIFTEEN_MINUTE" or sym not in symbols:
            return []
        stamps = _session_bars(start, end)
        px = symbols[sym](len(stamps))
        return [(t, p, p * 1.001, p * 0.999, p, 1e5) for t, p in zip(stamps, px)]
    return fetch


def test_warmup_fetches_enough_15_minute_history_for_the_regime_model(monkeypatch):
    """The window assumed 75 bars a session (the 5-minute clock). On the
    15-minute clock it held about 900 bars; the regime HMM needs 1,500 just
    to fit, so live ran the fallback classifier while the backtest ran the
    HMM."""
    r = _runner()
    r.cfg.regime.n_restarts, r.cfg.regime.n_iter = 1, 25   # keep the one fit quick
    rng = np.random.default_rng(0)
    walk = {"NIFTY": lambda n: 24_000 * np.exp(np.cumsum(rng.normal(0, 0.002, n)))}
    monkeypatch.setattr(r.broker_adapter, "historical_candles", _fake_15min(walk))
    r._warmup_from_history(decide_bars=3)
    assert len(r.histories["NIFTY"]) >= r.warmup_bars_needed()
    assert r.warmup_bars_needed() >= r.cfg.regime.timeframe_bars * r.cfg.regime.train_window
    assert r.engine.detector._params is not None, "regime HMM never fitted"


def test_warmup_leaves_no_risk_state_behind(monkeypatch):
    """Replayed decisions mark today's book at old prices. A replayed peak
    became the drawdown reference and a replayed slide latched the hard kill,
    so the first live bar flattened a book that had lost nothing."""
    r = _runner()
    r.broker.positions = {"HDFCBANK": Position("HDFCBANK", 1000, 1000.0)}
    slide = {"HDFCBANK": lambda n: np.linspace(2500.0, 1000.0, n)}
    monkeypatch.setattr(r.broker_adapter, "historical_candles", _fake_15min(slide))
    before = r.engine.risk.state_dict()
    r._warmup_from_history(lookback_bars=300, decide_bars=300)
    assert r.engine.risk.state_dict() == before
    assert r.engine.risk.hwm is None and r.engine.risk.dd_killed is False


def test_warmup_skips_the_candle_still_forming(monkeypatch):
    r = _runner()
    now = now_ist()
    monkeypatch.setattr(r.broker_adapter, "historical_candles",
                        _fake_15min({"HDFCBANK": lambda n: np.full(n, 1500.0)}))
    r._warmup_from_history(lookback_bars=300, decide_bars=1)
    h = r.histories["HDFCBANK"]
    if len(h):
        last = h.ts[-1].astype("datetime64[s]").astype(datetime)
        assert last + timedelta(minutes=15) <= now + timedelta(seconds=1)


def test_seeding_skips_todays_daily_candle(monkeypatch):
    """A restart during the session could seed today's half-formed daily
    candle as a finished day; the panel then kept it and dropped the full
    row built from live bars, so a shock later that day was never seen."""
    r = _runner()
    today = now_ist().date()

    def daily(sym, interval, start, end):
        if interval != "ONE_DAY":
            return []
        # weekdays, plus today whatever day it is: the candle still forming
        days = [today - timedelta(days=k) for k in range(300, 0, -1)]
        days = [d for d in days if d.weekday() < 5] + [today]
        return [(datetime.combine(d, time(0, 0)), 100.0, 101.0, 99.0, 100.0, 1e6)
                for d in days]

    monkeypatch.setattr(r.broker_adapter, "historical_candles", daily)
    r._seed_daily_panels()
    panel_sleeves = [s for s in r.engine.strategies if getattr(s, "_panel", None)]
    assert panel_sleeves
    for s in panel_sleeves:
        newest = max(dq[-1][0] for dq in s._panel.values())
        assert newest < today.isoformat(), s.name


# ------------------------------------------------ held symbols need a mark
def test_held_symbol_without_a_price_halts_the_bar(monkeypatch, db):
    r = _runner()
    r.broker.positions = {"HDFCBANK": Position("HDFCBANK", 1000, 1500.0)}
    calls = []
    monkeypatch.setattr(r.engine, "decide", lambda state: calls.append(state) or None)
    r.step(datetime(2026, 9, 21, 11, 0), {})
    assert calls == [], "the bar was decided with a held position valued at zero"
    from qsdash.models import EngineStatus
    db.expire_all()
    assert db.get(EngineStatus, 1).status == "halted"


def test_held_symbol_is_marked_from_its_last_recorded_bar(db):
    db.add(MarketBar(symbol="HDFCBANK", tf_minutes=15, ts=datetime(2026, 9, 18, 15, 15),
                     open=1510.0, high=1515.0, low=1505.0, close=1512.5, volume=1e5))
    db.add(MarketBar(symbol="HDFCBANK", tf_minutes=15, ts=datetime(2026, 9, 18, 15, 0),
                     open=1500.0, high=1505.0, low=1495.0, close=1501.0, volume=1e5))
    db.commit()
    r = _runner()
    r.broker.positions = {"HDFCBANK": Position("HDFCBANK", 1000, 1500.0)}
    r._seed_marks_for_held()
    assert r._last_prices["HDFCBANK"] == 1512.5



# ------------------------------------------------ review follow-ups
def test_a_cap_cut_made_while_down_is_rebased_not_read_as_a_drawdown():
    """The deployable cap is read from runtime_config at start. Measured
    against the restored drawdown reference, a 20% cut latched the hard kill
    on the first bar and flattened the book."""
    ts = datetime(2026, 9, 21, 11, 0)
    first = _runner()
    first.engine.risk.pre_decide(ts, 1_000_000.0)
    first._persist_engine_state(ts)

    second = _runner()
    second.deployable_cap_abs = 800_000.0            # what _load_runtime_config applies
    second._resume_engine()
    eq = second.effective_equity(second.broker.equity({}))
    pre = second.engine.risk.pre_decide(ts + timedelta(days=1), eq)
    assert pre.kill_reason is None and pre.drawdown == pytest.approx(0.0)
    assert second.engine.risk.hwm == pytest.approx(800_000.0)


def test_a_paper_capital_reset_at_start_is_rebased():
    ts = datetime(2026, 9, 21, 11, 0)
    first = _runner()
    first.engine.risk.pre_decide(ts, 1_000_000.0)
    first.engine.risk.pre_decide(ts, 950_000.0)      # a real 5% drawdown
    first._persist_engine_state(ts)

    second = _runner()
    second.broker.cash = 950_000.0
    second._startup_raw_equity = 950_000.0           # set when the reset is applied
    second.broker.cash = 1_900_000.0
    second._resume_engine()
    pre = second.engine.risk.pre_decide(ts + timedelta(days=1), 1_900_000.0)
    assert pre.drawdown == pytest.approx(0.05), "the real drawdown is kept, in proportion"


def test_the_state_row_follows_the_process_not_a_runtime_mode_switch(db):
    ts = datetime(2026, 9, 21, 11, 0)
    r = _runner()
    r._state_key = "live"                            # a live process ...
    r.mode = "paper"                                 # ... switched to paper at run time
    r._persist_engine_state(ts)
    db.expire_all()
    assert db.get(EngineState, "live") is not None
    assert db.get(EngineState, "paper") is None


def test_restored_cooldowns_survive_the_restart():
    """end_warmup drops cooldowns on flat symbols, and a cooldown only sits
    on a symbol just stopped out, i.e. flat. After a restore it ran anyway."""
    ts = datetime(2026, 9, 21, 11, 0)
    first = _runner()
    first.engine.risk.register_stop_hits({("trend", "HDFCBANK"): "stop"})
    first._persist_engine_state(ts)
    second = _runner()
    second._resume_engine()
    assert second.engine.risk.in_cooldown("trend", "HDFCBANK")


def test_a_holding_outside_the_universe_does_not_halt_decisions(monkeypatch):
    r = _runner()
    r.broker.positions = {"NIFTY30SEP25FUT": Position("NIFTY30SEP25FUT", 65, 24_000.0)}
    alerts = []
    monkeypatch.setattr("qsdash.bridge.live.notify_alert",
                        lambda *a, **k: alerts.append(k.get("kind")))
    calls = []
    monkeypatch.setattr(r.engine, "decide", lambda state: calls.append(state) or _no_orders(state))
    for k in range(2):
        r.step(datetime(2026, 9, 21, 11, 0) + timedelta(minutes=15 * k), {})
    assert len(calls) == 2, "a foreign holding stopped every decision, the kill switch included"
    assert "NIFTY30SEP25FUT" not in calls[0].positions
    assert alerts.count("foreign_holding") == 1


def _no_orders(state):
    from quantsys.core.types import Decision, RegimeState
    return Decision(ts=state.ts, equity=state.equity, tier_name="T1",
                    regime=RegimeState("calm_range", {}, 1.0, {}), signals=(), kelly={},
                    vol_scaler=1.0, risk_frac_eff=0.0, targets=(), orders=())


def test_warmup_reads_the_book_once(monkeypatch):
    """The replay read positions and equity from the broker on every decided
    bar; on the live broker any one failed read aborted the warm-up."""
    r = _runner()
    reads = []

    class _Once:
        cash = 1_000_000.0

        @property
        def positions(self):
            reads.append(1)
            if len(reads) > 1:
                raise AssertionError("second broker read during warm-up")
            return {}

        def equity(self, prices):
            raise AssertionError("warm-up must not ask the broker for equity")

    r.broker = _Once()
    monkeypatch.setattr(r.broker_adapter, "historical_candles",
                        _fake_15min({"HDFCBANK": lambda n: np.full(n, 1500.0)}))
    r._warmup_from_history(lookback_bars=300, decide_bars=50)
    assert reads == [1]



def test_only_the_live_runner_turns_off_equity_shorts():
    from qsdash.bridge.live import LiveRunner

    from quantsys.config import load_config

    for mode, allowed in (("live", False), ("paper", True)):
        r = LiveRunner.__new__(LiveRunner)
        r.cfg, r.mode = load_config(CFG), mode
        r._apply_live_constraints()
        assert r.cfg.sizing.allow_equity_shorts is allowed, mode



def test_paper_fill_needs_a_bar_in_the_decided_bucket(monkeypatch, db):
    """The paper broker filled at the last close of a symbol that did not
    print in the bucket, a price from before the decision. The backtest
    refuses that fill; paper now does too."""
    from qsdash.models import FillRow

    from quantsys.core.types import Bar, ExecutionStyle, OrderIntent, Urgency

    r = _runner()
    ts = datetime(2026, 9, 21, 11, 0)
    r._last_prices = {"HDFCBANK": 1500.0, "ICICIBANK": 1200.0}
    order = [OrderIntent(s, 10, ExecutionStyle.MARKET_SINGLE, Urgency.NORMAL, "trend", "entry")
             for s in ("HDFCBANK", "ICICIBANK")]
    monkeypatch.setattr(r.engine, "decide",
                        lambda state: dataclasses.replace(_no_orders(state), orders=tuple(order)))
    batch = {"HDFCBANK": Bar(ts, 1500.0, 1500.0, 1500.0, 1500.0, 1e5)}   # ICICIBANK silent
    before = db.query(FillRow).filter(FillRow.mode == "paper").count()
    r.step(ts, batch)
    assert set(r.broker.positions) == {"HDFCBANK"}
    db.query(FillRow).filter(FillRow.mode == "paper").delete()
    from qsdash.models import PositionRow
    db.query(PositionRow).filter(PositionRow.mode == "paper").delete()
    db.commit()
    assert before == 0
