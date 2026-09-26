"""Operator commands that move the risk engine's references.

The handler runs against a stub runner, and every session is rolled back, so
nothing here reaches the shared test database or picks up commands queued by
other tests.
"""

from __future__ import annotations

from datetime import datetime

import pytest
from qsdash.bridge.commands import CommandConsumer, _Reject
from qsdash.bus import make_sync_publisher
from qsdash.config import settings
from qsdash.db import SessionLocal
from qsdash.models import BacktestRun, Command, RuntimeConfig

from quantsys.config.schema import DrawdownConfig
from quantsys.risk.engine import RiskEngine

_TS = datetime(2026, 9, 21, 10, 0)


class _Broker:
    def __init__(self, cash: float) -> None:
        self.cash = cash
        self.positions: dict = {}

    def equity(self, prices) -> float:
        return self.cash


class _Runner:
    """The surface CommandConsumer uses, over a real RiskEngine."""

    def __init__(self, cash: float) -> None:
        self.mode = "paper"
        self.broker = _Broker(cash)
        self.engine = type("E", (), {})()
        self.engine.risk = RiskEngine(DrawdownConfig(), 0.006, set())
        self.deployable_cap_frac: float | None = None
        self.deployable_cap_abs: float | None = None

    def current_prices(self) -> dict:
        return {}

    def effective_equity(self, raw: float) -> float:
        e = raw
        if self.deployable_cap_frac is not None:
            e = min(e, raw * self.deployable_cap_frac)
        if self.deployable_cap_abs is not None:
            e = min(e, self.deployable_cap_abs)
        return e

    def reset_paper_capital(self, capital: float) -> None:
        self.broker.cash = capital


def _apply(runner: _Runner, kind: str, payload: dict | None = None) -> dict:
    consumer = CommandConsumer(runner, make_sync_publisher(SessionLocal))
    sess = SessionLocal()
    try:
        return consumer._apply(Command(kind=kind, payload=payload or {}), sess)
    finally:
        sess.rollback()
        sess.close()


def test_clear_halt_clears_only_the_reconciliation_freeze():
    r = _Runner(1_000_000.0)
    risk = r.engine.risk
    risk.pre_decide(_TS, 1_000_000.0)
    risk.reconcile({"X": 5}, {"X": 0})
    risk.dd_killed = True
    _apply(r, "clear_halt")
    assert risk.halted_reason is None
    assert risk.dd_killed is True, "clearing a halt must not re-arm a kill"
    assert risk.hwm == 1_000_000.0, "clearing a halt must not reset the drawdown"


def test_rearm_clears_the_kill_and_measures_drawdown_afresh():
    r = _Runner(790_000.0)
    risk = r.engine.risk
    risk.pre_decide(_TS, 1_000_000.0)
    assert risk.pre_decide(_TS, 790_000.0).kill_reason == "max_drawdown"
    _apply(r, "rearm_dd_kill")
    pre = risk.pre_decide(_TS, 790_000.0)
    assert pre.kill_reason is None and pre.drawdown == 0.0


def test_cutting_deployable_capital_is_not_booked_as_a_drawdown():
    """A 20% cut of the deployable cap used to read as a 20% drawdown:
    the hard kill fired and the book was flattened."""
    r = _Runner(1_000_000.0)
    risk = r.engine.risk
    risk.pre_decide(_TS, 1_000_000.0)
    _apply(r, "set_deployable_cap", {"deployable_cap_abs": 800_000.0})
    pre = risk.pre_decide(_TS, r.effective_equity(r.broker.equity({})))
    assert pre.kill_reason is None
    assert pre.drawdown == pytest.approx(0.0)
    assert risk.hwm == pytest.approx(800_000.0)


def test_new_paper_capital_scales_the_references():
    r = _Runner(1_000_000.0)
    risk = r.engine.risk
    risk.pre_decide(_TS, 1_000_000.0)
    risk.pre_decide(_TS, 950_000.0)          # a real 5% drawdown, kept in proportion
    r.broker.cash = 950_000.0
    _apply(r, "set_paper_capital", {"capital": 1_900_000.0})
    assert risk.hwm == pytest.approx(2_000_000.0)
    assert risk.pre_decide(_TS, 1_900_000.0).drawdown == pytest.approx(0.05)


def test_capital_change_with_unreadable_equity_leaves_references_alone():
    r = _Runner(1_000_000.0)
    risk = r.engine.risk
    risk.pre_decide(_TS, 1_000_000.0)

    def boom(prices):
        raise RuntimeError("broker read failed")

    r.broker.equity = boom
    _apply(r, "set_deployable_cap", {"deployable_cap_abs": 800_000.0})
    assert risk.hwm == 1_000_000.0


def test_rebaseline_live_book_is_refused_without_the_live_broker():
    r = _Runner(1_000_000.0)
    risk = r.engine.risk
    risk.reconcile({"X": 5}, {"X": 0})
    with pytest.raises(_Reject, match="live execution broker"):
        _apply(r, "rebaseline_live_book")
    assert risk.halted_reason is not None, "a refused rebaseline must not clear the halt"
    assert "note" not in _apply(r, "clear_halt")   # no live book here to re-baseline


_CAPS = ("deployable_cap_frac", "deployable_cap_abs")


def _apply_reading_caps(runner: _Runner, kind: str, payload: dict, seed=()):
    """_apply, plus the deployable caps in runtime_config before and after,
    read inside the command's session (rolled back like everything here).
    A rejection is returned, not raised."""
    consumer = CommandConsumer(runner, make_sync_publisher(SessionLocal))
    sess = SessionLocal()

    def caps() -> dict:
        out = {}
        for k in _CAPS:
            row = sess.query(RuntimeConfig).filter(RuntimeConfig.key == k).first()
            out[k] = None if row is None else row.value["v"]
        return out

    try:
        sess.add_all(seed)
        sess.flush()
        before = caps()
        try:
            result: dict | _Reject = consumer._apply(Command(kind=kind, payload=payload), sess)
        except _Reject as e:
            result = e
        return result, before, caps()
    finally:
        sess.rollback()
        sess.close()


def test_go_live_caps_apply_once_the_switch_is_made(monkeypatch):
    """The API wrote the caps before the gate ran; the handler now applies
    them after the switch, re-basing like set_deployable_cap does."""
    monkeypatch.setenv("QS_LIVE_ARMED", "1")
    monkeypatch.setattr(settings, "angel_webhook_secret", "s" * 40)
    r = _Runner(1_000_000.0)
    r.supports_live = r.adapter_connected = True
    risk = r.engine.risk
    risk.pre_decide(_TS, 1_000_000.0)
    passing = BacktestRun(label="real", metrics={
        "is_synthetic": False, "sharpe_oos": 1.4, "sharpe_deflated": 0.98,
        "monte_carlo": {"p_sharpe_negative": 0.02}})
    out, _, after = _apply_reading_caps(
        r, "set_mode", {"target_mode": "live", "deployable_cap_frac": 0.05}, seed=[passing])
    assert not isinstance(out, _Reject), out
    assert r.mode == "live" and r.deployable_cap_frac == 0.05
    assert after["deployable_cap_frac"] == 0.05
    pre = risk.pre_decide(_TS, r.effective_equity(r.broker.equity({})))
    assert pre.kill_reason is None and pre.drawdown == pytest.approx(0.0)


def test_refused_go_live_leaves_the_caps_alone():
    r = _Runner(1_000_000.0)             # paper runner: the gate refuses
    out, before, after = _apply_reading_caps(
        r, "set_mode", {"target_mode": "live", "deployable_cap_frac": 0.05})
    assert isinstance(out, _Reject) and "live gate refused" in str(out)
    assert r.mode == "paper" and r.deployable_cap_frac is None
    assert after == before
