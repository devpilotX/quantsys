"""Live-path hardening for the Angel adapter and the OMS.

Each test pins a defect that could send a wrong order, send an order twice,
leak a credential, or hide the true book. Fake transports only: nothing here
touches the network, and every test builds its own broker and book.
"""

from __future__ import annotations

import importlib.util
import logging
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest

from quantsys.core.types import (
    ExecutionStyle,
    Instrument,
    InstrumentKind,
    OrderIntent,
    Urgency,
    now_ist,
)
from quantsys.execution.angelone import AngelOneBroker
from quantsys.execution.broker import BrokerError, BrokerOrder, OrderStatus
from quantsys.execution.oms import OMS

_MON = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
        "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")


def _expiry(d: date) -> str:
    return f"{d.day:02d}{_MON[d.month - 1]}{d.year}"


def _master() -> list[dict]:
    """Fresh instrument master per call: one cash equity, one index future
    (the NIFTY-FUT alias resolves to it) and one index option."""
    exp = _expiry(now_ist().date() + timedelta(days=10))
    return [
        {"symbol": "SBIN-EQ", "name": "SBIN", "token": "3045", "exch_seg": "NSE",
         "instrumenttype": "", "lotsize": "1", "tick_size": "5"},
        {"symbol": f"NIFTY{exp}FUT", "name": "NIFTY", "token": "222",
         "exch_seg": "NFO", "instrumenttype": "FUTIDX", "expiry": exp,
         "lotsize": "65", "tick_size": "10"},
        {"symbol": f"NIFTY{exp}24000CE", "name": "NIFTY", "token": "333",
         "exch_seg": "NFO", "instrumenttype": "OPTIDX", "expiry": exp,
         "lotsize": "65", "tick_size": "5"},
    ]


def _dated_future() -> str:
    return next(r["symbol"] for r in _master() if r["instrumenttype"] == "FUTIDX")


def _option() -> str:
    return next(r["symbol"] for r in _master() if r["instrumenttype"] == "OPTIDX")


class BookTransport:
    """SmartConnect stand-in with a real order book. placeOrder books the
    order, cancelOrder marks it cancelled, and the knobs inject the failure
    modes the adapter must survive."""

    def __init__(self) -> None:
        self.placed: list[dict] = []
        self.cancels: list[str] = []
        self.events: list[tuple[str, str]] = []
        self.book: dict[str, dict] = {}
        self.book_reads = 0
        self.reads_at_place: list[int] = []
        self.calls = 0
        self._seq = 0
        self.position_resp: object = {"status": True, "data": None}
        self.lose_response = False     # book the order, then raise (response lost)
        self.refuse_connect = 0        # raise before booking, this many times
        self.reject_place = False      # SDK returns None: API said status false
        self.book_error = False        # orderBook raises
        self.booked_status = "open"
        self.honour_cancel = True      # False: cancel accepted, order stays working
        self.fill_on_cancel = 0        # shares that fill while the cancel is in flight

    def generateSession(self, code, mpin, totp):
        self.calls += 1
        return {"status": True, "data": {"refreshToken": "rt"}}

    def getfeedToken(self):
        return "ft"

    def rmsLimit(self):
        self.calls += 1
        return {"status": True, "data": {"availablecash": "1500000"}}

    def position(self):
        self.calls += 1
        return self.position_resp

    def orderBook(self):
        self.calls += 1
        self.book_reads += 1
        if self.book_error:
            raise ConnectionError("order book read timed out")
        return {"status": True, "data": [dict(r) for r in self.book.values()]}

    def placeOrder(self, params):
        self.calls += 1
        self.placed.append(dict(params))
        self.reads_at_place.append(self.book_reads)
        self.events.append(("place", params["ordertag"]))
        if self.refuse_connect > 0:
            self.refuse_connect -= 1
            raise ConnectionError("connect timed out")
        if self.reject_place:
            return None
        self._seq += 1
        oid = f"BRK{self._seq}"
        self.book[oid] = {
            "orderid": oid, "ordertag": params["ordertag"],
            "tradingsymbol": params["tradingsymbol"],
            "symboltoken": params["symboltoken"], "exchange": params["exchange"],
            "transactiontype": params["transactiontype"],
            "quantity": params["quantity"], "status": self.booked_status,
            "filledshares": "0", "averageprice": "0",
        }
        if self.lose_response:
            raise ConnectionError("read timed out")
        return oid  # the real SDK returns the bare order id

    def cancelOrder(self, oid, variety):
        self.calls += 1
        self.cancels.append(oid)
        self.events.append(("cancel", oid))
        row = self.book[oid]
        if self.honour_cancel:
            row["filledshares"] = str(int(row["filledshares"]) + self.fill_on_cancel)
            row["status"] = "cancelled"
        return {"status": True, "data": {"orderid": oid}}


def _adapter(t: BookTransport | None = None) -> AngelOneBroker:
    b = AngelOneBroker(api_key="k", client_code="c", mpin="1234",
                       totp_secret="JBSWY3DPEHPK3PXP",
                       transport=t if t is not None else BookTransport(),
                       instrument_master=_master())
    b.connect()
    b.lookup_pause_s = 0.0   # tests never wait for the order book to catch up
    return b


def _oms(b: AngelOneBroker) -> OMS:
    oms = OMS(b)
    oms.retry_pause_s = 0.0
    oms.cancel_poll_s = 0.0
    return oms


def _intent(qty: int, style=ExecutionStyle.MARKET_SINGLE, sym="SBIN-EQ",
            urgency=Urgency.NORMAL) -> OrderIntent:
    return OrderIntent(sym, qty, style, urgency, "trend", "test")


_T1 = datetime(2026, 6, 12, 9, 20)
_T2 = datetime(2026, 6, 12, 9, 25)
_PX = {"SBIN-EQ": 550.0}


# ------------------------------------------------ 6: timeouts, credential leak
_needs_sdk = pytest.mark.skipif(importlib.util.find_spec("SmartApi") is None,
                                reason="smartapi-python not installed (broker extra)")

# fake credentials, distinctive enough to spot in a leaked log line
_CREDS = {"api_key": "APIKEY-9f3c2d71", "client_code": "C7412589",  # gitleaks:allow
          "mpin": "4719", "totp_secret": "JBSWY3DPEHPK3PXP"}
_TOTP_CODE = "802615"
_JWT = "eyJhbGciOiJIUzUxMiJ9.bGVha21l.c2lnbmF0dXJl"


def _offline_get(*args, **kwargs):
    raise OSError("network disabled in tests")


@_needs_sdk
@pytest.mark.parametrize("failure", ["raise", "status_false"])
def test_sdk_error_paths_never_write_credentials(failure, tmp_path, monkeypatch, capfd):
    """Drives the SDK's real SmartConnect._request error paths. The SDK logs
    request headers (Bearer JWT, X-PrivateKey) and params (client code, MPIN,
    TOTP) via logzero, to stderr and to ./logs/<date>/app.log. None of it may
    reach a log record, the console, a file, or our own error text."""
    import pyotp
    import requests

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(requests, "get", _offline_get)

    def fake_request(method, url, **kw):
        if failure == "raise":
            # worst case: a transport error whose text carries the request
            raise requests.ConnectionError(
                f"{method} {url} failed; sent {kw.get('data')} {kw.get('params')} "
                f"with {kw.get('headers')}")
        return SimpleNamespace(status_code=200, content=(
            b'{"status": false, "message": "Invalid totp", '
            b'"errorcode": "AB1050", "data": null}'))

    monkeypatch.setattr(requests, "request", fake_request)
    monkeypatch.setattr(pyotp.TOTP, "now", lambda self: _TOTP_CODE)
    records: list[logging.LogRecord] = []
    real_make = logging.Logger.makeRecord

    def spy(self, *a, **k):
        rec = real_make(self, *a, **k)
        records.append(rec)
        return rec

    monkeypatch.setattr(logging.Logger, "makeRecord", spy)

    b = AngelOneBroker(**_CREDS, instrument_master=[])
    errors: list[str] = []
    try:
        b.connect()
    except BrokerError as e:
        errors.append(str(e))
    # a live session: every request header now carries the Bearer JWT
    b._transport.setAccessToken(_JWT)
    try:
        b.positions()
    except BrokerError as e:
        errors.append(str(e))

    secrets = [*_CREDS.values(), _TOTP_CODE, _JWT]
    leaked = [r.getMessage() for r in records
              if any(s in r.getMessage() for s in secrets)]
    assert leaked == []
    out, err = capfd.readouterr()
    assert [s for s in secrets if s in out or s in err] == []
    assert not (tmp_path / "logs").exists()
    files = [p for p in tmp_path.rglob("*") if p.is_file()
             and any(s in p.read_text(errors="ignore") for s in secrets)]
    assert files == []
    assert len(errors) >= 1
    assert [e for e in errors if any(s in e for s in secrets)] == []


@_needs_sdk
def test_sdk_requests_carry_explicit_connect_and_read_timeouts(tmp_path, monkeypatch):
    """The SDK default is a flat 7 s per request; a hung endpoint must not
    block the decision loop for longer than the read budget we choose."""
    import requests

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(requests, "get", _offline_get)
    seen: list[object] = []

    def fake_request(method, url, **kw):
        seen.append(kw.get("timeout"))
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(requests, "request", fake_request)
    b = AngelOneBroker(api_key="k", client_code="c", mpin="1234",
                       totp_secret="JBSWY3DPEHPK3PXP", instrument_master=[])
    with pytest.raises(BrokerError):
        b.connect()
    assert seen == [(3.05, 10)]

    seen.clear()
    b = AngelOneBroker(api_key="k", client_code="c", mpin="1234",
                       totp_secret="JBSWY3DPEHPK3PXP", instrument_master=[],
                       http_timeout=(1.0, 2.5))
    with pytest.raises(BrokerError):
        b.connect()
    assert seen == [(1.0, 2.5)]


# ------------------------------------------------------ 7: LIMIT price guard
@pytest.mark.parametrize("px", [None, 0.0, -5.0, float("nan"), float("inf")])
def test_limit_order_without_a_valid_price_is_refused(px):
    """A LIMIT with no price used to go out at "0": an unprotected market order."""
    t = BookTransport()
    b = _adapter(t)
    with pytest.raises(BrokerError) as ei:
        b.place(BrokerOrder("D1", "SBIN-EQ", "BUY", 5, ExecutionStyle.LIMIT_SINGLE,
                            Urgency.NORMAL, limit_price=px))
    assert ei.value.retryable is False
    assert t.placed == []


@pytest.mark.parametrize("sym,side,px,expected", [
    ("SBIN-EQ", "BUY", 550.07, "550.05"),
    ("SBIN-EQ", "BUY", 550.0500000000001, "550.05"),   # observed: on tick + noise
    ("SBIN-EQ", "BUY", 550.0499999999998, "550.05"),   # one ulp below the tick
    ("NIFTY-FUT", "BUY", 24000.07, "24000.0"),
    ("NIFTY-FUT", "SELL", 24000.07, "24000.1"),
    ("NIFTY-FUT", "SELL", 24000.100000000002, "24000.1"),
    ("NIFTY-FUT", "BUY", 24000.100000000002, "24000.1"),
    ("NIFTY-FUT", "SELL", 24000.099999999995, "24000.1"),
])
def test_limit_price_rounds_to_tick_away_from_aggression(sym, side, px, expected):
    """Buys round down and sells round up, so rounding never makes an order
    more aggressive, and the wire price carries no float noise."""
    t = BookTransport()
    b = _adapter(t)
    b.place(BrokerOrder("D1", sym, side, 65 if sym == "NIFTY-FUT" else 5,
                        ExecutionStyle.LIMIT_SINGLE, Urgency.NORMAL, limit_price=px))
    assert t.placed[-1]["price"] == expected
    assert t.placed[-1]["ordertype"] == "LIMIT"


# ------------------------------------------------- 8: failed reads are errors
@pytest.mark.parametrize("resp", [
    {"status": False, "message": "Invalid Token", "errorcode": "AG8001", "data": None},
    {"status": True, "data": "not a list"},
    {"status": True, "data": [{"tradingsymbol": "SBIN-EQ", "symboltoken": "3045",
                               "exchange": "NSE", "netqty": "ten", "netprice": "1"}]},
    {"status": True, "data": [{"tradingsymbol": "SBIN-EQ", "symboltoken": "3045",
                               "exchange": "NSE", "netqty": "nan", "netprice": "1"}]},
    {"data": [{"tradingsymbol": "SBIN-EQ", "netqty": "10", "netprice": "550"}]},
    None,
], ids=["status_false", "data_not_list", "qty_garbage", "qty_nan", "no_status", "none"])
def test_positions_raise_on_error_or_malformed_payloads(resp):
    """An error payload used to come back as [] (a flat book), so the engine
    re-entered every target on top of the real positions."""
    t = BookTransport()
    t.position_resp = resp
    b = _adapter(t)
    with pytest.raises(BrokerError):
        b.positions()


def test_positions_empty_book_is_flat():
    t = BookTransport()
    t.position_resp = {"status": True, "message": "SUCCESS", "data": None}
    assert _adapter(t).positions() == []


def test_order_book_error_payload_raises():
    t = BookTransport()
    b = _adapter(t)
    t.orderBook = lambda: {"status": False, "message": "Invalid Token", "data": None}
    with pytest.raises(BrokerError):
        b.open_orders()


# ---------------------------------------------- 10a: deterministic client ids
def test_client_ids_carry_year_and_source_and_fit_the_ordertag():
    from quantsys.execution.oms import SOURCE_DECISION, SOURCE_FLATTEN

    a = OMS.client_id(_T1, 7, 3)
    assert a != OMS.client_id(_T1.replace(year=2027), 7, 3)
    assert a == OMS.client_id(_T1, 7, 3)
    assert a == OMS.client_id(_T1, 7, 3, source=SOURCE_DECISION)
    assert OMS.client_id(_T1, 7, 3, source=SOURCE_FLATTEN) != a
    worst = OMS.client_id(datetime(2099, 12, 31, 23, 59), 999, 99,
                          source=SOURCE_FLATTEN) + "-s9"
    assert len(worst) <= 20
    # a field that outgrows its width would collide with a neighbour; refuse
    with pytest.raises(ValueError):
        OMS.client_id(_T1, 1000, 0)
    with pytest.raises(ValueError):
        OMS.client_id(_T1, 1, 100)


# ------------------------------------ 10b: lost responses are never resent
@pytest.mark.parametrize("booked_status", ["open", "exchange review"])
def test_lost_place_response_is_recovered_by_ordertag_not_resent(booked_status):
    """Sim C: the broker accepted the order, the response was lost, the OMS
    retried with the same ordertag and a second real order went out."""
    t = BookTransport()
    t.lose_response = True
    t.booked_status = booked_status
    b = _adapter(t)
    oms = _oms(b)
    mos = oms.submit_intents([_intent(10)], b.instruments(), _T1, _PX)
    assert len(t.placed) == 1
    s = mos[0].slices[0]
    assert s.placed and oms._submitted[s.client_order_id] == "BRK1"
    oms.pump(b.instruments(), _PX)
    assert len(t.placed) == 1
    # the recovered mapping serves later cancels
    b.cancel(s.client_order_id)
    assert t.cancels == ["BRK1"]


def test_failed_send_is_resent_only_after_two_empty_lookups():
    t = BookTransport()
    t.refuse_connect = 1
    b = _adapter(t)
    oms = _oms(b)
    oms.submit_intents([_intent(10)], b.instruments(), _T1, _PX)
    assert len(t.placed) == 2
    assert t.reads_at_place == [0, 2]   # two order-book lookups before the resend


def test_unreadable_book_after_failed_send_never_resends():
    t = BookTransport()
    t.lose_response = True
    t.book_error = True
    b = _adapter(t)
    oms = _oms(b)
    mos = oms.submit_intents([_intent(10)], b.instruments(), _T1, _PX)
    assert len(t.placed) == 1
    for _ in range(3):
        oms.pump(b.instruments(), _PX)
    assert len(t.placed) == 1
    assert not mos[0].slices[0].placed


def test_pump_never_resends_a_refused_slice():
    t = BookTransport()
    t.reject_place = True
    b = _adapter(t)
    oms = _oms(b)
    oms.submit_intents([_intent(10)], b.instruments(), _T1, _PX)
    for _ in range(3):
        oms.pump(b.instruments(), _PX)
    assert len(t.placed) == 1


# ------------------------------------------ 11: resting orders are superseded
def _rest_then_resize(t: BookTransport):
    b = _adapter(t)
    oms = _oms(b)
    lim = ExecutionStyle.LIMIT_SINGLE
    first = oms.submit_intents([_intent(10, lim)], b.instruments(), _T1, _PX)
    tag1 = first[0].slices[0].client_order_id
    second = oms.submit_intents([_intent(10, lim)], b.instruments(), _T2, _PX)
    return oms, tag1, second


def test_new_parent_cancels_the_resting_order_first():
    """The next bar used to send the same delta again under a new id while
    the first LIMIT still rested, so both could fill."""
    t = BookTransport()
    oms, tag1, second = _rest_then_resize(t)
    assert t.events[:2] == [("place", tag1), ("cancel", "BRK1")]
    assert len(t.placed) == 2 and t.events[2][0] == "place"
    assert len(second) == 1


def test_new_parent_skipped_when_the_cancel_is_not_confirmed():
    t = BookTransport()
    t.honour_cancel = False
    oms, tag1, second = _rest_then_resize(t)
    assert t.cancels == ["BRK1"]
    assert len(t.placed) == 1
    assert second == []
    assert [s for s, _ in oms.last_skipped] == ["SBIN-EQ"]


def test_new_parent_skipped_when_the_resting_order_filled_during_cancel():
    """The decision's delta predates those fills; sending it would overshoot."""
    t = BookTransport()
    t.fill_on_cancel = 4
    oms, tag1, second = _rest_then_resize(t)
    assert t.cancels == ["BRK1"]
    assert len(t.placed) == 1
    assert [s for s, _ in oms.last_skipped] == ["SBIN-EQ"]


# ------------------------- 11b: fills observed before the bar's position read
@pytest.mark.parametrize("urgency", [Urgency.KILL, Urgency.RISK_REDUCING, Urgency.NORMAL])
def test_fill_observed_before_the_position_read_does_not_hold_back_the_exit(urgency):
    """Only supersession observed fills, so every order that filled looked
    unseen when the next parent on its symbol arrived, and that parent (a
    kill or a stop exit too) waited a bar."""
    t = BookTransport()
    b = _adapter(t)
    oms = _oms(b)
    oms.submit_intents([_intent(10)], b.instruments(), _T1, _PX, positions={})
    t.book["BRK1"].update(status="complete", filledshares="10", averageprice="550")
    oms.observe_working()             # the next bar, just before its position read
    reads = t.book_reads
    oms.submit_intents([_intent(-10, urgency=urgency)], b.instruments(), _T2, _PX,
                       positions={"SBIN-EQ": 10})
    assert [p["transactiontype"] for p in t.placed] == ["BUY", "SELL"]
    assert oms.last_skipped == []
    assert t.cancels == [] and t.book_reads == reads   # a finished order needs neither


def test_fill_after_the_observation_still_holds_back_the_next_order():
    """The exit was sized from a read that predates this fill: sending it
    would oversell."""
    t = BookTransport()
    b = _adapter(t)
    oms = _oms(b)
    oms.submit_intents([_intent(-5, ExecutionStyle.LIMIT_SINGLE)], b.instruments(), _T1,
                       _PX, positions={"SBIN-EQ": 10})
    oms.observe_working()                       # resting, nothing filled yet
    t.book["BRK1"].update(filledshares="4")     # fills after the position read
    oms.submit_intents([_intent(-10, urgency=Urgency.KILL)], b.instruments(), _T2, _PX,
                       positions={"SBIN-EQ": 10})
    assert len(t.placed) == 1
    assert [s for s, _ in oms.last_skipped] == ["SBIN-EQ"]


def test_observation_reads_the_order_book_once_for_every_working_slice():
    t = BookTransport()
    b = _adapter(t)
    oms = _oms(b)
    lim = ExecutionStyle.LIMIT_SINGLE
    oms.submit_intents([_intent(10, lim), _intent(65, lim, sym="NIFTY-FUT")],
                       b.instruments(), _T1, {**_PX, "NIFTY-FUT": 24000.0}, positions={})
    reads = t.book_reads
    oms.observe_working()
    assert t.book_reads == reads + 1
    t.book["BRK1"].update(status="complete", filledshares="10")
    oms.observe_working()
    oms.observe_working()          # only NIFTY-FUT is still working
    assert t.book_reads == reads + 3
    t.book["BRK2"].update(status="cancelled")
    oms.observe_working()
    oms.observe_working()          # nothing left to observe: no read at all
    assert t.book_reads == reads + 4


class _NoOrderBook:
    """A venue that answers per-order status but has no order-book read."""

    def __init__(self, inner: AngelOneBroker) -> None:
        self._inner = inner

    def __getattr__(self, name: str):
        if name == "open_orders":
            raise AttributeError(name)
        return getattr(self._inner, name)


def test_flatten_ids_carry_the_attempt_within_the_minute():
    """Flatten ids have minute resolution: a second flatten in the same
    minute re-derived the first one's ids, so nothing was sent. The attempt
    goes in the seq field; a decision's ids are unchanged."""
    from quantsys.execution.oms import SOURCE_FLATTEN

    t = BookTransport()
    b = _adapter(t)
    oms = _oms(b)
    insts = b.instruments()
    idx = sorted(insts).index("SBIN-EQ")
    exit_ = _intent(-10, urgency=Urgency.KILL)
    held = {"SBIN-EQ": 10}
    first = oms.submit_intents([exit_], insts, _T1, _PX, positions=held,
                               source=SOURCE_FLATTEN)
    again = oms.submit_intents([exit_], insts, _T1 + timedelta(seconds=20), _PX,
                               positions=held, source=SOURCE_FLATTEN)
    assert [mo.parent_id for mo in first + again] == [
        OMS.client_id(_T1, idx, 0, source=SOURCE_FLATTEN),
        OMS.client_id(_T1, idx, 1, source=SOURCE_FLATTEN)]
    assert len(t.placed) == 2
    (d,) = oms.submit_intents([_intent(5)], insts, _T2, _PX, positions={})
    assert d.parent_id == OMS.client_id(_T2, idx, 0)


def test_observation_asks_per_order_without_an_order_book_read():
    t = BookTransport()
    b = _adapter(t)
    oms = _oms(_NoOrderBook(b))
    oms.submit_intents([_intent(10)], b.instruments(), _T1, _PX, positions={})
    t.book["BRK1"].update(status="complete", filledshares="10")
    oms.observe_working()
    oms.submit_intents([_intent(-10, urgency=Urgency.KILL)], b.instruments(), _T2, _PX,
                       positions={"SBIN-EQ": 10})
    assert [p["transactiontype"] for p in t.placed] == ["BUY", "SELL"]
    assert oms.last_skipped == []


# ------------------------------------------------------ 12: product types
def test_product_type_follows_the_holding_period():
    """INTRADAY futures and options are auto squared off before the close
    while the engine holds them for days."""
    t = BookTransport()
    b = _adapter(t)
    insts = b.instruments()
    assert insts["NIFTY-FUT"].kind == InstrumentKind.FUTURE
    assert insts[_option()].kind == InstrumentKind.OPTION
    b.place(BrokerOrder("F1", "NIFTY-FUT", "BUY", 65, ExecutionStyle.MARKET_SINGLE,
                        Urgency.NORMAL))
    b.place(BrokerOrder("O1", _option(), "BUY", 65, ExecutionStyle.MARKET_SINGLE,
                        Urgency.NORMAL))
    b.place(BrokerOrder("E1", "SBIN-EQ", "BUY", 5, ExecutionStyle.MARKET_SINGLE,
                        Urgency.NORMAL))
    assert [p["producttype"] for p in t.placed] == ["CARRYFORWARD", "CARRYFORWARD",
                                                    "DELIVERY"]


def test_equity_sell_that_could_open_a_short_is_refused():
    """Cash-segment shorts cannot be carried overnight in India; a DELIVERY
    sell with no holdings is a short the engine would then hold for days."""
    t = BookTransport()
    b = _adapter(t)
    with pytest.raises(BrokerError) as ei:
        b.place(BrokerOrder("E2", "SBIN-EQ", "SELL", 5, ExecutionStyle.MARKET_SINGLE,
                            Urgency.NORMAL))
    assert ei.value.retryable is False
    assert t.placed == []


def test_equity_sell_that_only_reduces_a_long_goes_as_delivery():
    t = BookTransport()
    b = _adapter(t)
    b.place(BrokerOrder("E3", "SBIN-EQ", "SELL", 5, ExecutionStyle.MARKET_SINGLE,
                        Urgency.NORMAL, reduce_only=True))
    assert t.placed[-1]["producttype"] == "DELIVERY"


def test_oms_marks_equity_sells_reduce_only_from_the_held_position():
    t = BookTransport()
    b = _adapter(t)
    oms = _oms(b)
    oms.submit_intents([_intent(-5)], b.instruments(), _T1, _PX,
                       positions={"SBIN-EQ": 10})
    assert [p["transactiontype"] for p in t.placed] == ["SELL"]
    oms.submit_intents([_intent(-15)], b.instruments(), _T2, _PX,
                       positions={"SBIN-EQ": 10})
    assert len(t.placed) == 1   # would open a short: refused at the adapter


# ------------------------------------------ 13: futures alias tradingsymbol
def test_futures_alias_orders_carry_the_dated_tradingsymbol():
    t = BookTransport()
    b = _adapter(t)
    insts = b.instruments()
    assert insts["NIFTY-FUT"].broker_symbol == _dated_future()
    assert insts["SBIN-EQ"].broker_symbol is None
    assert Instrument("X").broker_symbol is None
    b.place(BrokerOrder("F2", "NIFTY-FUT", "BUY", 65, ExecutionStyle.MARKET_SINGLE,
                        Urgency.NORMAL))
    assert t.placed[-1]["tradingsymbol"] == _dated_future()
    assert t.placed[-1]["symboltoken"] == "222"


def test_positions_map_back_to_the_engine_symbol_by_token():
    """Angel reports the dated contract; the engine holds NIFTY-FUT. Keyed by
    tradingsymbol the engine saw itself flat and re-bought every bar."""
    t = BookTransport()
    t.position_resp = {"status": True, "data": [
        {"tradingsymbol": _dated_future(), "symboltoken": "222", "exchange": "NFO",
         "netqty": "65", "netprice": "24000.5"},
        {"tradingsymbol": "SBIN-EQ", "symboltoken": "3045", "exchange": "NSE",
         "netqty": "-3", "netprice": "550"},
    ]}
    pos = {p.symbol: p.qty for p in _adapter(t).positions()}
    assert pos == {"NIFTY-FUT": 65, "SBIN-EQ": -3}


# ------------------------------------------------- 14: order status mapping
def _book_row(status: str, filled: str, qty: str = "100") -> dict:
    return {"orderid": "BRK9", "ordertag": "T9", "tradingsymbol": "SBIN-EQ",
            "symboltoken": "3045", "exchange": "NSE", "transactiontype": "BUY",
            "quantity": qty, "status": status, "filledshares": filled,
            "averageprice": "550"}


def test_unknown_broker_status_is_not_new_and_not_terminal():
    t = BookTransport()
    b = _adapter(t)
    t.book["BRK9"] = _book_row("exchange review", "0")
    (o,) = b.open_orders()
    assert o.status is not OrderStatus.NEW
    assert o.status.terminal is False
    assert o.status.value == "UNKNOWN"


@pytest.mark.parametrize("status,filled,expected,terminal", [
    ("cancelled", "40", "CANCELLED", True),    # a finished partial fill
    ("open", "40", "PARTIAL", False),
    ("complete", "100", "FILLED", True),
    ("open", "0", "SUBMITTED", False),
])
def test_fill_state_is_derived_from_filledshares(status, filled, expected, terminal):
    t = BookTransport()
    b = _adapter(t)
    t.book["BRK9"] = _book_row(status, filled)
    (o,) = b.open_orders()
    assert o.status.value == expected
    assert o.status.terminal is terminal
    assert o.filled_qty == int(filled)


def test_rate_limit_timeouts_fail_observably(monkeypatch):
    """acquire() returning False used to be ignored and the call went out
    anyway, past the budget Angel enforces."""
    t = BookTransport()
    b = _adapter(t)
    monkeypatch.setattr(b._generic, "acquire", lambda n=1.0, timeout=None: False)
    before = t.calls
    for call in (b.funds, b.positions, b.open_orders, b.connect):
        with pytest.raises(BrokerError, match="rate limit"):
            call()
    assert t.calls == before


# ------------------------------------------- engine-side reconciliation map
def test_reconcile_books_compares_maps_and_skips_working_symbols():
    from quantsys.execution.reconcile import reconcile_books

    r = reconcile_books({"SBIN-EQ": 0, "INFY": 5}, {"SBIN-EQ": 10, "INFY": 5},
                        skip={"SBIN-EQ"})
    assert r.ok and r.skipped == ["SBIN-EQ"]
    assert "SBIN-EQ" not in r.internal and "SBIN-EQ" not in r.broker
    r = reconcile_books({"INFY": 5}, {"INFY": 4, "TCS": 1})
    assert not r.ok
    assert sorted(r.mismatches) == ["INFY: internal=5 broker=4",
                                    "TCS: internal=0 broker=1"]
