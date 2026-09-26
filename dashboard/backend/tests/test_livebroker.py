"""LiveExecutionBroker: the write-ahead order journal, the engine-side
intended book, reads that fail loudly, and the cash-equity short guard.

Mock Angel transport only. Rows land in the suite's SQLite DB; every test
starts from and leaves behind an empty live book.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta

import pytest
from qsdash.bridge.commands import CommandConsumer, _Reject
from qsdash.bus import make_sync_publisher
from qsdash.db import SessionLocal, now_ist
from qsdash.models import Command, FillRow, OrderRow, PositionRow, RiskEvent, RuntimeConfig

from quantsys.config.schema import DrawdownConfig
from quantsys.core.types import (
    Decision,
    ExecutionStyle,
    OrderIntent,
    RegimeState,
    Urgency,
)
from quantsys.execution.angelone import AngelOneBroker
from quantsys.execution.broker import BrokerError
from quantsys.risk.engine import RiskEngine

# today's session: a restart expires DAY orders from earlier sessions
_TS = datetime.combine(now_ist().date(), time(9, 20))
_PX = {"SBIN-EQ": 550.0}


class Book:
    """SmartConnect stand-in with a real order book and a settable position
    book. placeOrder books the order; knobs inject the failure modes."""

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.cancels: list[str] = []
        self.book: dict[str, dict] = {}
        self.rows: list[dict] = []
        self.on_place = None          # called with params before booking
        self.lose_response = False
        self.book_error = False
        self.funds_error = False
        self.positions_error = False
        self.reads: list[str] = []    # position and order-book reads, in order
        self._seq = 0

    def generateSession(self, code, mpin, totp):
        return {"status": True, "data": {"refreshToken": "rt"}}

    def getfeedToken(self):
        return "ft"

    def rmsLimit(self):
        if self.funds_error:
            raise ConnectionError("rms read timed out")
        return {"status": True, "data": {"availablecash": "1500000"}}

    def position(self):
        self.reads.append("position")
        if self.positions_error:
            return {"status": False, "message": "Invalid Token", "data": None}
        return {"status": True, "data": [dict(r) for r in self.rows]}

    def orderBook(self):
        self.reads.append("orderBook")
        if self.book_error:
            raise ConnectionError("order book read timed out")
        return {"status": True, "data": [dict(r) for r in self.book.values()]}

    def placeOrder(self, params):
        if self.on_place is not None:
            self.on_place(params)
        self.placed.append(dict(params))
        self._seq += 1
        oid = f"BRK{self._seq}"
        self.book[oid] = {
            "orderid": oid, "ordertag": params["ordertag"],
            "tradingsymbol": params["tradingsymbol"],
            "symboltoken": params["symboltoken"], "exchange": params["exchange"],
            "transactiontype": params["transactiontype"],
            "quantity": params["quantity"], "status": "open",
            "filledshares": "0", "averageprice": "0",
        }
        if self.lose_response:
            raise ConnectionError("read timed out")
        return oid

    def cancelOrder(self, oid, variety):
        self.cancels.append(oid)
        self.book[oid]["status"] = "cancelled"
        return {"status": True, "data": {"orderid": oid}}

    def hold(self, qty: int, px: float = 550.0) -> None:
        self.rows = [{"tradingsymbol": "SBIN-EQ", "symboltoken": "3045",
                      "exchange": "NSE", "netqty": str(qty), "netprice": str(px)}]


def _master() -> list[dict]:
    return [{"symbol": "SBIN-EQ", "name": "SBIN", "token": "3045", "exch_seg": "NSE",
             "instrumenttype": "", "lotsize": "1", "tick_size": "5"}]


def _live(t: Book):
    from qsdash.bridge.livebroker import LiveExecutionBroker

    adapter = AngelOneBroker(api_key="k", client_code="c", mpin="1",
                             totp_secret="JBSWY3DPEHPK3PXP", transport=t,
                             instrument_master=_master())
    adapter.connect()
    adapter.lookup_pause_s = 0.0
    lb = LiveExecutionBroker(adapter, adapter.instruments(),
                             make_sync_publisher(SessionLocal))
    lb.oms.retry_pause_s = 0.0
    lb.oms.cancel_poll_s = 0.0
    return lb


def _decision(*intents: OrderIntent, ts: datetime = _TS,
              kill_reason: str | None = None) -> Decision:
    return Decision(ts=ts, equity=1_500_000.0, tier_name="T1",
                    regime=RegimeState("calm_trend", {}, 1.0, {}), signals=(),
                    kelly={}, vol_scaler=1.0, risk_frac_eff=0.0, targets=(),
                    orders=tuple(intents), kill_reason=kill_reason)


def _buy(qty: int, style=ExecutionStyle.MARKET_SINGLE) -> OrderIntent:
    return OrderIntent("SBIN-EQ", qty, style, Urgency.NORMAL, "trend", "entry")


@pytest.fixture(autouse=True)
def _empty_live_book(db):
    def wipe():
        for model in (FillRow, OrderRow, PositionRow, RiskEvent):
            db.query(model).filter(model.mode == "live").delete(synchronize_session=False)
        db.query(RuntimeConfig).filter(RuntimeConfig.key == "live_book_baseline").delete(
            synchronize_session=False)
        db.commit()

    wipe()
    yield
    wipe()


def _rows(db) -> list[OrderRow]:
    db.expire_all()
    return db.query(OrderRow).filter(OrderRow.mode == "live").order_by(OrderRow.id).all()


# ------------------------------------------------- 8: reads fail loudly
def test_positions_map_raises_instead_of_reporting_a_flat_book():
    t = Book()
    lb = _live(t)
    t.positions_error = True
    with pytest.raises(BrokerError):
        lb.positions_map()


def test_equity_uses_recent_funds_and_refuses_stale_ones():
    """A failed funds read used to leave equity = MTM only, which reads as a
    near-total drawdown: kill switch, then a market flatten."""
    now = [1000.0]
    t = Book()
    t.hold(10)
    lb = _live(t)
    lb.clock = lambda: now[0]
    assert lb.equity(_PX) == pytest.approx(1_500_000 + 10 * 550.0)
    t.funds_error = True
    now[0] += 60.0                       # past the snapshot TTL, funds still fresh
    assert lb.equity(_PX) == pytest.approx(1_500_000 + 10 * 550.0)
    now[0] += 200.0                      # last good funds value now too old
    with pytest.raises(BrokerError):
        lb.equity(_PX)


# ------------------------------------------- 9: the engine's own book
def test_intended_book_is_the_baseline_plus_fills_recorded_after_it(db):
    t = Book()
    t.hold(10)
    db.add(FillRow(mode="live", symbol="SBIN-EQ", qty=3, price=540.0, ts=now_ist()))
    db.commit()                          # before the baseline: already in the snapshot
    lb = _live(t)
    lb.load_open_state()
    base = (db.query(PositionRow)
            .filter(PositionRow.mode == "live", PositionRow.status == "open").all())
    assert [(p.symbol, p.qty) for p in base] == [("SBIN-EQ", 10)]
    assert lb.intended_book() == {"SBIN-EQ": 10}
    db.add(FillRow(mode="live", symbol="SBIN-EQ", qty=-4, price=560.0, ts=now_ist()))
    db.commit()
    assert lb.intended_book() == {"SBIN-EQ": 6}
    # a restart reloads the persisted baseline, it does not re-read the broker
    t.hold(999)
    again = _live(t)
    again.load_open_state()
    assert again.intended_book() == {"SBIN-EQ": 6}
    r = again.reconcile()
    assert not r.ok and r.mismatches == ["SBIN-EQ: internal=6 broker=999"]


# ------------------------------------------------ 10c: write-ahead journal
def test_order_row_is_committed_before_the_broker_call(db):
    t = Book()
    seen: list[str | None] = []

    def journal_state(params):
        s = SessionLocal()
        try:
            row = (s.query(OrderRow)
                   .filter(OrderRow.client_order_id == params["ordertag"]).one_or_none())
            seen.append(None if row is None else row.status)
        finally:
            s.close()

    t.on_place = journal_state
    lb = _live(t)
    lb.execute(_decision(_buy(10)), None, _PX, _TS)
    assert seen == ["PENDING_SUBMIT"]
    (row,) = _rows(db)
    assert (row.status, row.broker_order_id) == ("SUBMITTED", "BRK1")


def test_restart_rederiving_a_sent_order_does_not_send_it_again(db):
    t = Book()
    _live(t).execute(_decision(_buy(10)), None, _PX, _TS)
    # restart: fresh OMS and adapter maps, same broker, same bar re-decided
    _live(t).execute(_decision(_buy(10)), None, _PX, _TS)
    assert len(t.placed) == 1
    assert len(_rows(db)) == 1


def test_pending_row_is_resolved_by_ordertag_after_a_restart(db):
    """The send reached Angel, the response was lost and the book was
    unreadable: the row stays PENDING_SUBMIT and nothing is resent. After a
    restart the ordertag lookup finds the order and maps it."""
    t = Book()
    t.lose_response = True
    t.book_error = True
    _live(t).execute(_decision(_buy(10)), None, _PX, _TS)
    assert len(t.placed) == 1
    (row,) = _rows(db)
    assert row.status == "PENDING_SUBMIT" and row.broker_order_id == ""

    t.lose_response = False
    t.book_error = False
    lb = _live(t)
    lb.load_open_state()
    (row,) = _rows(db)
    assert (row.status, row.broker_order_id) == ("SUBMITTED", "BRK1")
    lb.execute(_decision(_buy(10)), None, _PX, _TS)
    assert len(t.placed) == 1
    lb.broker.cancel(row.client_order_id)       # 10d: mapping restored
    assert t.cancels == ["BRK1"]


def test_open_orders_are_reloaded_into_the_adapter_after_a_restart(db):
    cid = "QD2606120920001-s0"
    db.add(OrderRow(client_order_id=cid, ts=now_ist(), mode="live", symbol="SBIN-EQ",
                    side="BUY", qty=10, status="SUBMITTED", broker_order_id="BRK7"))
    db.commit()
    t = Book()
    t.book["BRK7"] = {"orderid": "BRK7", "ordertag": cid, "tradingsymbol": "SBIN-EQ",
                      "symboltoken": "3045", "exchange": "NSE",
                      "transactiontype": "BUY", "quantity": "10", "status": "open",
                      "filledshares": "0", "averageprice": "0"}
    lb = _live(t)
    lb.load_open_state()
    lb.broker.cancel(cid)
    assert t.cancels == ["BRK7"]
    assert lb.broker.order_status(cid).broker_order_id == "BRK7"


def test_every_send_path_journals_first(db):
    """execute, pump (TWAP slices) and flatten_all all write the row first."""
    t = Book()
    t.hold(12)
    unjournaled: list[str] = []

    def check(params):
        s = SessionLocal()
        try:
            if s.query(OrderRow).filter(
                    OrderRow.client_order_id == params["ordertag"]).count() != 1:
                unjournaled.append(params["ordertag"])
        finally:
            s.close()

    t.on_place = check
    lb = _live(t)
    lb.execute(_decision(_buy(8, ExecutionStyle.SLICE_TWAP)), None, _PX, _TS)
    for _ in range(4):
        lb.pump(_PX)
    lb.flatten_all(_PX, _TS + timedelta(minutes=1))
    assert len(t.placed) >= 3
    assert unjournaled == []


# ------------------------------------- 10a: decision vs flatten, same minute
def test_flatten_in_the_decision_minute_sends_its_own_order(db):
    """flatten_all derived the decision's parent id for the same minute and
    was dropped as 'already handled'."""
    t = Book()
    lb = _live(t)
    lb.execute(_decision(_buy(10)), None, _PX, _TS)
    t.book["BRK1"].update(status="complete", filledshares="10", averageprice="550")
    t.hold(10)
    lb.flatten_all(_PX, _TS + timedelta(seconds=30))
    assert [(p["transactiontype"], p["quantity"]) for p in t.placed] == [
        ("BUY", "10"), ("SELL", "10")]
    assert len({p["ordertag"] for p in t.placed}) == 2


# ------------------------------------------------ 11: resting LIMIT orders
def test_next_bar_cancels_the_resting_limit_before_resizing(db):
    t = Book()
    lb = _live(t)
    lim = ExecutionStyle.LIMIT_SINGLE
    lb.execute(_decision(_buy(10, lim)), None, _PX, _TS)
    nxt = _TS + timedelta(minutes=5)
    lb.execute(_decision(_buy(10, lim), ts=nxt), None, _PX, nxt)
    assert t.cancels == ["BRK1"]
    assert len(t.placed) == 2
    first, second = _rows(db)
    assert first.status == "CANCELLED" and second.status == "SUBMITTED"


# -------------------------------------------- 12: cash-equity short guard
def test_live_equity_sell_beyond_the_held_long_is_refused_and_flagged(db):
    """A DELIVERY sell with no holdings is a short that cannot be carried."""
    t = Book()
    t.hold(4)
    lb = _live(t)
    sell = OrderIntent("SBIN-EQ", -10, ExecutionStyle.MARKET_SINGLE, Urgency.NORMAL,
                       "trend", "flip")
    lb.execute(_decision(sell), None, _PX, _TS)
    assert [(p["transactiontype"], p["quantity"], p["producttype"])
            for p in t.placed] == [("SELL", "4", "DELIVERY")]
    ev = (db.query(RiskEvent)
          .filter(RiskEvent.mode == "live", RiskEvent.kind == "equity_short_refused")
          .one())
    assert ev.symbol == "SBIN-EQ"


def test_live_equity_short_entry_sends_nothing(db):
    t = Book()
    lb = _live(t)
    sell = OrderIntent("SBIN-EQ", -10, ExecutionStyle.MARKET_SINGLE, Urgency.NORMAL,
                       "trend", "entry")
    lb.execute(_decision(sell), None, _PX, _TS)
    assert t.placed == []
    assert db.query(RiskEvent).filter(
        RiskEvent.mode == "live", RiskEvent.kind == "equity_short_refused").count() == 1


# ------------------------------- fills observed before the bar's position read
def _sell(qty: int, style=ExecutionStyle.MARKET_SINGLE, urgency=Urgency.NORMAL,
          reason: str = "exit") -> OrderIntent:
    return OrderIntent("SBIN-EQ", -qty, style, urgency, "trend", reason)


def test_refresh_reads_the_order_book_before_the_positions(db):
    """An order sized from this position read may only be held back for
    fills after it, so the working orders are observed first."""
    t = Book()
    lb = _live(t)
    lb.execute(_decision(_buy(10, ExecutionStyle.LIMIT_SINGLE)), None, _PX, _TS)
    t.reads.clear()
    lb.refresh_snapshot()
    assert t.reads == ["orderBook", "position"]


def test_stop_exit_after_a_filled_entry_goes_out_on_the_next_bar(db):
    """Only the next order's supersession noticed the entry's fill, and it
    then held that order back a bar: the stop exit went out a bar late."""
    t = Book()
    lb = _live(t)
    lb.execute(_decision(_buy(10)), None, _PX, _TS)
    t.book["BRK1"].update(status="complete", filledshares="10", averageprice="550")
    t.hold(10)
    nxt = _TS + timedelta(minutes=15)
    lb.refresh_snapshot()                        # the next bar's read
    stop = _sell(10, urgency=Urgency.RISK_REDUCING, reason="stop")
    lb.execute(_decision(stop, ts=nxt), None, _PX, nxt)
    assert [(p["transactiontype"], p["quantity"]) for p in t.placed] == [
        ("BUY", "10"), ("SELL", "10")]
    assert db.query(RiskEvent).filter(
        RiskEvent.mode == "live", RiskEvent.kind == "order_skipped").count() == 0


def test_kill_exits_what_the_fresh_book_holds(db):
    """The engine sizes its kill from the bar's read. A resting trim that
    fills after that read makes the engine's delta oversell into a short, so
    the kill cancels working orders, re-reads the book and exits what is
    held, like flatten_all."""
    t = Book()
    t.hold(10)
    lb = _live(t)
    lb.execute(_decision(_sell(5, ExecutionStyle.LIMIT_SINGLE, reason="trim")),
               None, _PX, _TS)                   # rests at the broker
    nxt = _TS + timedelta(minutes=15)
    lb.refresh_snapshot()                        # the bar's read: long 10
    t.book["BRK1"].update(filledshares="4")      # the trim fills 4 after it
    t.hold(6)
    kill = _sell(10, urgency=Urgency.KILL, reason="kill_switch")
    lb.execute(_decision(kill, ts=nxt, kill_reason="max_drawdown"), None, _PX, nxt)
    assert t.cancels == ["BRK1"]
    assert [(p["transactiontype"], p["quantity"], p["ordertype"]) for p in t.placed] == [
        ("SELL", "5", "LIMIT"), ("SELL", "6", "MARKET")]


def test_a_reread_within_the_bar_keeps_the_bars_observation(db):
    """The decision was sized from the bar's read. A re-read after the
    snapshot's TTL ran out must not mark later fills as seen: the scale-in
    below would then go out on top of the resting order that just filled."""
    now = [1000.0]
    t = Book()
    t.hold(10)
    lb = _live(t)
    lb.clock = lambda: now[0]
    lim = ExecutionStyle.LIMIT_SINGLE
    lb.execute(_decision(_buy(5, lim)), None, _PX, _TS)       # rests at the broker
    now[0] += 900
    nxt = _TS + timedelta(minutes=15)
    lb.refresh_snapshot()                        # the bar's read: long 10, nothing filled
    t.book["BRK1"].update(status="complete", filledshares="5", averageprice="545")
    t.hold(15)                                   # fills while the engine decides
    now[0] += 60                                 # past the snapshot TTL
    lb.execute(_decision(_buy(5), ts=nxt), None, _PX, nxt)   # sized from long 10
    assert len(t.placed) == 1
    assert [s for s, _ in lb.oms.last_skipped] == ["SBIN-EQ"]


def test_fill_seen_before_its_postback_is_skipped_then_reported_if_lost(db):
    """The order book can show a fill a moment before its postback books
    it. Reading that as a mismatch would freeze the engine; a postback that
    never arrives must still surface."""
    now = [1000.0]
    t = Book()
    lb = _live(t)
    lb.clock = lambda: now[0]
    lb.refresh_snapshot()                        # baseline: flat
    lb.execute(_decision(_buy(10)), None, _PX, _TS)
    t.book["BRK1"].update(status="complete", filledshares="10", averageprice="550")
    t.hold(10)
    now[0] += 900
    r = lb.reconcile(lb.refresh_snapshot())      # the postback is still in flight
    assert r.ok and r.skipped == ["SBIN-EQ"]
    (row,) = _rows(db)
    assert row.status == "FILLED"                # closed from the order book
    now[0] += 900                                # a bar later: the postback was lost
    r = lb.reconcile(lb.refresh_snapshot())
    assert not r.ok and r.mismatches == ["SBIN-EQ: internal=0 broker=10"]


def test_fill_booked_by_its_postback_reconciles_at_once(db):
    now = [1000.0]
    t = Book()
    lb = _live(t)
    lb.clock = lambda: now[0]
    lb.refresh_snapshot()
    lb.execute(_decision(_buy(10)), None, _PX, _TS)
    t.book["BRK1"].update(status="complete", filledshares="10", averageprice="550")
    t.hold(10)
    now[0] += 900
    snap = lb.refresh_snapshot()
    (row,) = _rows(db)
    row.status, row.filled_qty = "FILLED", 10     # the postback lands before the check
    db.add(FillRow(order_id=row.id, client_order_id=row.client_order_id, mode="live",
                   symbol="SBIN-EQ", qty=10, price=550.0, ts=now_ist()))
    db.commit()
    r = lb.reconcile(snap)
    assert r.ok and r.skipped == []


# ----------------------------------------- flatten results and operator commands
class _LiveRunner:
    """The surface CommandConsumer uses, over this module's live broker."""

    def __init__(self, lb) -> None:
        self.mode = "live"
        self.broker = lb
        self.engine = type("E", (), {})()
        self.engine.risk = RiskEngine(DrawdownConfig(), 0.006, set())

    def current_prices(self) -> dict:
        return dict(_PX)


def _command(runner: _LiveRunner, kind: str, payload: dict | None = None) -> dict:
    consumer = CommandConsumer(runner, make_sync_publisher(SessionLocal))
    sess = SessionLocal()
    try:
        return consumer._apply(Command(kind=kind, payload=payload or {}), sess)
    finally:
        sess.rollback()
        sess.close()


def _refuse_orders(params):
    raise ConnectionError("RMS: order rejected / connection reset")


def test_flatten_reports_a_refused_exit_as_still_open(db):
    """A refused exit was in neither sent nor blocked, so the flatten
    command read as done with the whole book open."""
    t = Book()
    t.hold(10)
    lb = _live(t)
    t.on_place = _refuse_orders
    out = lb.flatten_all(_PX, _TS)
    assert out["sent"] == [] and list(out["blocked"]) == ["SBIN-EQ"]
    why = out["blocked"]["SBIN-EQ"]
    assert "failed" in why and "RMS: order rejected" in why
    ev = (db.query(RiskEvent)
          .filter(RiskEvent.mode == "live", RiskEvent.kind == "flatten_blocked").one())
    assert (ev.symbol, ev.severity) == ("SBIN-EQ", "crit")


def test_flatten_command_with_a_refused_exit_is_rejected(db):
    t = Book()
    t.hold(10)
    runner = _LiveRunner(_live(t))
    t.on_place = _refuse_orders
    with pytest.raises(_Reject, match="flatten incomplete, still open: SBIN-EQ"):
        _command(runner, "flatten", {"reason": "operator"})


def test_flatten_retried_in_the_same_minute_sends_the_exit(db):
    """The retry derived the refused exit's id, the journal held its REJECTED
    row, and nothing was sent."""
    t = Book()
    t.hold(10)
    lb = _live(t)
    t.on_place = _refuse_orders
    first = lb.flatten_all(_PX, _TS)
    assert list(first["blocked"]) == ["SBIN-EQ"]
    t.on_place = None                          # the broker takes orders again
    second = lb.flatten_all(_PX, _TS + timedelta(seconds=20))
    assert second == {"sent": ["SBIN-EQ"], "blocked": {}}
    assert [(p["transactiontype"], p["quantity"]) for p in t.placed] == [("SELL", "10")]
    assert len({r.client_order_id for r in _rows(db)}) == 2


def test_rebaseline_command_ends_a_freeze_that_clear_halt_cannot(db):
    """clear_halt was re-latched on the next bar, because the intended book
    never changes, and rebaseline() had no caller."""
    t = Book()
    lb = _live(t)
    runner = _LiveRunner(lb)
    risk = runner.engine.risk
    lb.refresh_snapshot()                      # baseline: flat
    t.hold(10)                                 # a fill no postback ever booked

    def bar() -> None:
        r = lb.reconcile(lb.refresh_snapshot())
        if not r.ok:
            risk.reconcile(r.internal, r.broker)

    bar()
    assert risk.halted_reason == "reconciliation: SBIN-EQ: internal=0 broker=10"
    assert "rebaseline_live_book" in _command(runner, "clear_halt")["note"]
    bar()
    assert risk.halted_reason is not None      # the mismatch persists: frozen again
    out = _command(runner, "rebaseline_live_book")
    assert out["baseline"] == {"SBIN-EQ": 10}
    assert risk.halted_reason is None
    bar()
    assert risk.halted_reason is None
    assert db.query(RiskEvent).filter(
        RiskEvent.mode == "live", RiskEvent.kind == "live_book_rebaselined").count() == 1


# --------------------------------------------- after_send vs a racing postback
def test_after_send_keeps_a_fill_the_postback_committed_first(db, monkeypatch):
    """after_send read the row, saw PENDING_SUBMIT and wrote SUBMITTED over a
    postback that committed FILLED in between; the symbol then dropped out
    of reconciliation until the next order on it or a restart."""
    from qsdash.bridge import livebroker as livebroker_module
    from sqlalchemy import event

    from quantsys.execution.broker import BrokerOrder, OrderAck, OrderStatus

    cid = "QD2609260920000-s0"
    db.add(OrderRow(client_order_id=cid, ts=now_ist(), mode="live", symbol="SBIN-EQ",
                    side="BUY", qty=10, status="PENDING_SUBMIT"))
    db.commit()
    lb = _live(Book())
    landed: list[bool] = []

    def postback(*_args) -> None:
        if landed:
            return
        landed.append(True)
        s = SessionLocal()
        try:
            row = s.query(OrderRow).filter(OrderRow.client_order_id == cid).one()
            row.status, row.filled_qty, row.broker_order_id = "FILLED", 10, "BRK1"
            s.commit()
        finally:
            s.close()

    def racing_session():
        """A session in which the postback commits just before the first write."""
        sess = SessionLocal()
        event.listen(sess, "before_flush", postback)
        event.listen(sess, "do_orm_execute", lambda st: postback() if st.is_update else None)
        return sess

    monkeypatch.setattr(livebroker_module, "SessionLocal", racing_session)
    order = BrokerOrder(cid, "SBIN-EQ", "BUY", 10, ExecutionStyle.MARKET_SINGLE,
                        Urgency.NORMAL)
    lb.after_send(order, OrderAck(cid, "BRK1", OrderStatus.SUBMITTED))
    (row,) = _rows(db)
    assert landed
    assert (row.status, row.filled_qty, row.broker_order_id) == ("FILLED", 10, "BRK1")
