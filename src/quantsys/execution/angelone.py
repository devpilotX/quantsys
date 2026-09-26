"""Angel One SmartAPI broker adapter.

The SmartConnect SDK and the websocket client are imported lazily (inside
methods) so this module loads — and the unit tests run — without the SDK or
any network. Inject a `transport` for tests; in production it defaults to the
real SmartConnect.

Assumptions (verify against the current SmartAPI docs at deploy time; they
drift):
- auth: SmartConnect.generateSession(client_code, mpin, totp) -> jwt/refresh;
  generateToken(refresh) to refresh. TOTP from ANGEL_TOTP_SECRET via pyotp.
- order: placeOrder(variety, tradingsymbol, symboltoken, transactiontype,
  exchange, ordertype, producttype, quantity, price). ordertag carries our
  client_order_id for postback correlation.
- rate limits: order ~10/s, historical ~3/s (token buckets enforce).
- MPP: market orders on some segments convert to a protected LIMIT; we treat
  any fill price as ground truth from the fill/postback, never assume.
- ordertag is also the recovery key: a placeOrder whose response is lost is
  looked up in the order book by ordertag before anything may send it again.

SDK containment. SmartConnect logs request headers (Bearer JWT, api key) and
params (client code, MPIN, TOTP) on every failed call, through logzero, to
stderr and to ./logs/<date>/app.log under the working directory. We mute its
loggers, stop its constructor creating that directory, give every request an
explicit (connect, read) timeout, and scrub every credential we hold from the
error text we raise.
"""

from __future__ import annotations

import logging
import math
import os
import sys
import threading
import time
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import ROUND_CEILING, ROUND_FLOOR, ROUND_HALF_EVEN, Decimal
from typing import Any

from quantsys.core.types import (
    ExecutionStyle,
    Instrument,
    InstrumentKind,
    Urgency,
    now_ist,
)
from quantsys.execution.broker import (
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    OrderAck,
    OrderStateUnknown,
    OrderStatus,
)
from quantsys.execution.ratelimit import TokenBucket

log = logging.getLogger("quantsys.angelone")

# (connect, read) seconds for every SmartAPI HTTP call; requests accepts the
# tuple. The SDK default is a flat 7 s. Connect fails fast on a dead route;
# read leaves room for a slow order book without stalling the decision loop.
HTTP_CONNECT_TIMEOUT_S = 3.05
HTTP_READ_TIMEOUT_S = 10.0
HTTP_TIMEOUT = (HTTP_CONNECT_TIMEOUT_S, HTTP_READ_TIMEOUT_S)

# Pause before each of the two order-book lookups that follow a send whose
# response was lost, so a just-accepted order has time to appear.
LOOKUP_PAUSE_S = 0.5

# A limit price within this many ticks of a grid point is that grid point:
# float arithmetic upstream leaves noise such as 550.0500000000001.
_TICK_SNAP = Decimal("1e-6")

# Anything not listed maps to OrderStatus.UNKNOWN (working, never resendable).
_STATUS_MAP = {
    "open": OrderStatus.SUBMITTED, "open pending": OrderStatus.SUBMITTED,
    "trigger pending": OrderStatus.SUBMITTED, "validation pending": OrderStatus.SUBMITTED,
    "put order req received": OrderStatus.SUBMITTED,
    "complete": OrderStatus.FILLED, "executed": OrderStatus.FILLED,
    "partially executed": OrderStatus.PARTIAL,
    "cancelled": OrderStatus.CANCELLED, "rejected": OrderStatus.REJECTED,
}

_EXCHANGE = {InstrumentKind.EQUITY: "NSE", InstrumentKind.FUTURE: "NFO",
             InstrumentKind.OPTION: "NFO", InstrumentKind.INDEX: "NSE"}

# Angel One getCandleData max days per request, by interval (their published
# limits). We chunk a long backfill into windows no larger than these.
_HIST_MAX_DAYS = {
    "ONE_MINUTE": 30, "THREE_MINUTE": 60, "FIVE_MINUTE": 100,
    "TEN_MINUTE": 100, "FIFTEEN_MINUTE": 200, "THIRTY_MINUTE": 200,
    "ONE_HOUR": 400, "ONE_DAY": 2000,
}


class AngelOneBroker:
    """Implements the Broker protocol against SmartAPI."""

    def __init__(self, *, api_key: str | None = None, client_code: str | None = None,
                 mpin: str | None = None, totp_secret: str | None = None,
                 transport=None, instrument_master=None,
                 http_timeout: tuple[float, float] = HTTP_TIMEOUT,
                 lookup_pause_s: float = LOOKUP_PAUSE_S):
        self.api_key = api_key or os.environ.get("ANGEL_API_KEY", "")
        self.client_code = client_code or os.environ.get("ANGEL_CLIENT_CODE", "")
        self.mpin = mpin or os.environ.get("ANGEL_PASSWORD", "")
        self.totp_secret = totp_secret or os.environ.get("ANGEL_TOTP_SECRET", "")
        self.http_timeout = http_timeout
        self.lookup_pause_s = lookup_pause_s
        self._transport = transport            # injected SmartConnect-like object
        self._instr_master = instrument_master  # injected for tests
        self._connected = False
        self._refresh_token = ""
        self._feed_token = ""
        self._last_totp = ""
        self._instruments: dict[str, Instrument] = {}
        self._token_to_symbol: dict[str, str] = {}
        # (exchange, token) -> engine symbol. The same token number can exist
        # on two segments, so a bare token is only trusted without an exchange.
        self._symbol_by_exch_token: dict[tuple[str, str], str] = {}
        self._orders = TokenBucket(10, 10, "orders")
        # 2/s, capacity 1 => no burst; Angel's historical limit trips on bursts
        # and on sustained 3/s, so stay conservative and lean on retry/backoff.
        self._hist = TokenBucket(2, 1, "historical")
        self._generic = TokenBucket(3, 3, "generic")
        self._client_to_broker: dict[str, str] = {}
        # client ids whose last send may have reached Angel without a readable
        # response; the next place() of such an id looks it up before sending.
        self._unresolved: set[str] = set()
        self._unknown_statuses: set[str] = set()

    # ----------------------------------------------------------- connect
    def _build_transport(self):
        if self._transport is not None:
            return self._transport
        try:
            smart_connect = _smartconnect_class()
        except ImportError as e:  # pragma: no cover - prod-only path
            raise BrokerError(
                "smartapi-python not installed; pip install smartapi-python") from e
        self._transport = smart_connect(api_key=self.api_key, timeout=self.http_timeout)
        _mute_sdk_logging()  # anything the constructor attached goes too
        return self._transport

    def _scrub(self, obj: object) -> str:
        """Text of ``obj`` with every credential this adapter holds masked.
        SDK exceptions and responses can carry request headers and params."""
        text = str(obj)
        t = self._transport
        for secret in (self.api_key, self.client_code, self.mpin, self.totp_secret,
                       self._last_totp, self._refresh_token, self._feed_token,
                       getattr(t, "access_token", None),
                       getattr(t, "refresh_token", None),
                       getattr(t, "feed_token", None)):
            # short values (test stubs, single digits) would mangle the text
            if isinstance(secret, str) and len(secret) >= 4:
                text = text.replace(secret, "***")
        return text

    def _login(self) -> None:
        """Authenticate (TOTP) and capture fresh access/refresh/feed tokens.
        Does NOT pull the instrument master — that is connect()'s job; a feed
        reconnect re-logins without re-pulling the universe (see
        reconnect_feed_session)."""
        import pyotp

        t = self._build_transport()
        if not (self.client_code and self.mpin and self.totp_secret):
            raise BrokerError("Angel One credentials missing in environment")
        code = pyotp.TOTP(self.totp_secret).now()
        self._last_totp = code
        _take(self._generic, 10, "generateSession")
        try:
            data = t.generateSession(self.client_code, self.mpin, code)
        except Exception as e:  # pragma: no cover
            raise BrokerError(f"generateSession failed: {self._scrub(e)}", retryable=True)
        if not data or not data.get("status", True):
            raise BrokerError(f"auth rejected: {self._scrub(data)}")
        self._refresh_token = (data.get("data") or {}).get("refreshToken", "")
        try:
            self._feed_token = t.getfeedToken()
        except Exception:
            self._feed_token = ""
        self._connected = True

    def connect(self) -> None:
        self._login()
        self.refresh_instruments()
        log.info("Angel One connected; %d instruments", len(self._instruments))

    def reconnect_feed_session(self) -> dict:
        """Re-login for fresh websocket tokens after a feed drop, WITHOUT
        re-pulling the instrument master (the universe is already loaded).
        Friday's token is dead by Monday, so the feed supervisor calls this
        before each reconnect. Returns the creds the websocket needs."""
        self._login()
        return {
            "auth_token": getattr(self._transport, "access_token", ""),
            "feed_token": self._feed_token,
            "api_key": self.api_key,
            "client_code": self.client_code,
        }

    def is_connected(self) -> bool:
        return self._connected

    def refresh_session(self) -> None:
        if self._transport is None or not self._refresh_token:
            raise BrokerError("not connected; cannot refresh")
        try:
            self._transport.generateToken(self._refresh_token)
        except Exception as e:  # pragma: no cover
            self._connected = False
            raise BrokerError(f"token refresh failed: {self._scrub(e)}", retryable=True)

    # ------------------------------------------------- instrument master
    def refresh_instruments(self) -> dict[str, Instrument]:
        """Pull lot size / tick / token from the instrument master. The master
        is the daily source of truth; config values are only warm-start
        fallbacks (see live_runner)."""
        master = self._load_master()
        out: dict[str, Instrument] = {}
        index_by_name: dict[str, Instrument] = {}
        for row in master:
            sym = row.get("symbol") or row.get("name")
            if not sym:
                continue
            kind = _infer_kind(row)
            inst = Instrument(
                symbol=sym,
                token=str(row.get("token", "")),
                exchange=row.get("exch_seg", _EXCHANGE.get(kind, "NSE")),
                kind=kind,
                lot_size=int(float(row.get("lotsize", 1) or 1)),
                tick_size=float(row.get("tick_size", 5) or 5) / 100.0,
                point_value=1.0,
            )
            out[sym] = inst
            self._token_to_symbol[inst.token] = sym
            self._symbol_by_exch_token[(inst.exchange, inst.token)] = sym
            name = row.get("name")
            # NSE equity indices only: the SAME index name appears on other
            # segments (e.g. NIFTY shows up on CDS as token '2') whose tokens
            # return no NSE candle data. Match the NSE row (AMXIDX token 99926000
            # for NIFTY) and keep the first, so resolution is deterministic.
            if (kind == InstrumentKind.INDEX and name
                    and row.get("exch_seg") == "NSE" and name not in index_by_name):
                index_by_name[name] = inst
        # Indices are ALSO resolvable by their NAME: config uses "NIFTY", but the
        # master's index symbol is "Nifty 50". Index rows win the name key over a
        # same-named non-index row, because only the AMXIDX token returns candle
        # data — the bare spot row (token 26000) returns NONE, which is what left
        # the regime HMM starved and stuck in `warmup`.
        for name, inst in index_by_name.items():
            out[name] = inst
            self._token_to_symbol[inst.token] = name
            self._symbol_by_exch_token[(inst.exchange, inst.token)] = name
        # Continuous near-month FUTURES aliases. The config universe uses static
        # rolling symbols ("NIFTY-FUT", "BANKNIFTY-FUT"), but the master only
        # carries DATED contracts ("NIFTY30JUN26FUT", ...). Resolve each
        # "<NAME>-FUT" to the nearest NON-EXPIRED index-future (FUTIDX) so the
        # engine gets a real token + data feed. Without this the index futures
        # have NO token -> no websocket subscription -> no bars -> they never
        # enter the tradeable universe (the "algo takes no index trades"
        # symptom). The alias re-rolls to the next contract each time the master
        # is (re)loaded at startup.
        fut_rows: dict[str, list[dict]] = {}
        for row in master:
            if (row.get("instrumenttype") or "").upper() == "FUTIDX" and row.get("name"):
                fut_rows.setdefault(row["name"].upper(), []).append(row)
        today = now_ist().date()
        for name, rows in fut_rows.items():
            dated = []
            for r in rows:
                d = _parse_expiry(r.get("expiry"))
                if d is not None:
                    dated.append((d, r))
            if not dated:
                continue
            dated.sort(key=lambda x: x[0])
            front = next((r for d, r in dated if d >= today), dated[-1][1])
            front_sym = front.get("symbol")
            front_inst = out.get(front_sym) if isinstance(front_sym, str) else None
            if front_inst is None:
                continue
            alias = f"{name}-FUT"
            # Orders must carry the dated contract's tradingsymbol; the alias
            # is ours, Angel does not know it.
            out[alias] = replace(front_inst, symbol=alias,
                                 broker_symbol=front_inst.broker_symbol or front_inst.symbol)
            self._token_to_symbol[front_inst.token] = alias
            self._symbol_by_exch_token[(front_inst.exchange, front_inst.token)] = alias
        self._instruments = out
        return out

    def _load_master(self) -> list[dict]:
        if self._instr_master is not None:
            return self._instr_master
        import json
        import urllib.request

        url = ("https://margincalculator.angelbroking.com/"
               "OpenAPI_File/files/OpenAPIScripMaster.json")
        _take(self._hist, 15, "instrument master")
        try:  # pragma: no cover - network
            with urllib.request.urlopen(url, timeout=30) as r:
                return json.loads(r.read().decode())
        except Exception as e:  # pragma: no cover
            raise BrokerError(f"instrument master fetch failed: {e}", retryable=True)

    def instruments(self) -> dict[str, Instrument]:
        return dict(self._instruments)

    # ----------------------------------------------------- historical data
    def historical_candles(self, symbol: str, interval: str,
                           start: datetime, end: datetime) -> list[tuple]:
        """Fetch OHLCV candles in [start, end] via getCandleData, chunked to
        Angel's per-request day limits and rate-limited. Returns
        (naive-IST ts, open, high, low, close, volume) tuples, ascending and
        de-duplicated. Read-only — never places an order.

        ``start``/``end`` are naive IST wall-clock (the engine's clock); Angel
        returns +05:30 stamps which we strip back to naive IST so backtest and
        live traverse identical timestamps.
        """
        inst = self._instruments.get(symbol)
        if inst is None or not inst.token:
            raise BrokerError(f"no instrument token for {symbol}")
        interval = interval.upper()
        max_days = _HIST_MAX_DAYS.get(interval)
        if max_days is None:
            raise BrokerError(f"unknown interval {interval!r}; one of "
                              f"{sorted(_HIST_MAX_DAYS)}")
        out: list[tuple] = []
        seen: set[datetime] = set()
        win_start = start
        while win_start <= end:
            win_end = min(end, win_start + timedelta(days=max_days - 1,
                                                     hours=23, minutes=59))
            params = {
                "exchange": inst.exchange,
                "symboltoken": inst.token,
                "interval": interval,
                "fromdate": win_start.strftime("%Y-%m-%d %H:%M"),
                "todate": win_end.strftime("%Y-%m-%d %H:%M"),
            }
            # Angel's historical endpoint rate-limits aggressively; a single
            # 429 must NOT abort the whole symbol. Retry the window with
            # exponential backoff so the limiter window resets.
            resp = None
            last_err: Exception | None = None
            for attempt in range(6):
                if not self._hist.acquire(timeout=30):
                    raise BrokerError("historical rate limit timeout", retryable=True)
                try:
                    resp = self._transport.getCandleData(params)
                    break
                except Exception as e:  # pragma: no cover - network
                    last_err = e
                    if attempt < 5:
                        # 2,4,8,16,32s — long enough to outlast a per-minute cap
                        time.sleep(min(32, 2 ** (attempt + 1)))
            if resp is None:
                raise BrokerError(f"getCandleData {symbol} failed after retries: "
                                  f"{self._scrub(last_err)}", retryable=True)
            for row in (resp or {}).get("data") or []:
                ts = datetime.fromisoformat(str(row[0])).replace(tzinfo=None)
                if ts in seen:
                    continue
                seen.add(ts)
                out.append((ts, float(row[1]), float(row[2]), float(row[3]),
                            float(row[4]), float(row[5])))
            win_start = win_end + timedelta(minutes=1)
        out.sort(key=lambda r: r[0])
        return out

    # ---------------------------------------------------------- read paths
    def _rows(self, resp: object, what: str) -> list[dict]:
        """The list payload of a SmartAPI read. Only an explicit success with
        no data means "nothing there"; an error payload or malformed data
        raises, because returning [] would read as a flat book."""
        if not isinstance(resp, dict) or resp.get("status") is not True:
            raise BrokerError(f"{what} failed: {self._scrub(resp)[:300]}", retryable=True)
        data = resp.get("data")
        if data is None:
            return []
        if not isinstance(data, list) or not all(isinstance(r, dict) for r in data):
            raise BrokerError(f"{what} returned malformed data: "
                              f"{self._scrub(data)[:300]}", retryable=True)
        return data

    def _engine_symbol(self, r: dict) -> str:
        """Engine symbol for a broker row, by (exchange, token) first: Angel
        reports the dated contract behind an alias such as NIFTY-FUT."""
        token = str(r.get("symboltoken") or "")
        exch = str(r.get("exchange") or "")
        if token:
            sym = (self._symbol_by_exch_token.get((exch, token)) if exch
                   else self._token_to_symbol.get(token))
            if sym:
                return sym
        return str(r.get("tradingsymbol") or "")

    # ------------------------------------------------------------- funds
    def funds(self) -> float:
        _take(self._generic, 10, "rmsLimit")
        try:
            rms = self._transport.rmsLimit()
        except Exception as e:  # pragma: no cover
            raise BrokerError(f"rmsLimit failed: {self._scrub(e)}", retryable=True)
        if not isinstance(rms, dict) or rms.get("status") is not True:
            raise BrokerError(f"rmsLimit failed: {self._scrub(rms)[:300]}", retryable=True)
        data = rms.get("data")
        if isinstance(data, dict):
            for key in ("availablecash", "net", "availableCash"):
                if key in data:
                    try:
                        value = float(data[key])
                    except (TypeError, ValueError):
                        continue
                    if math.isfinite(value):
                        return value
        raise BrokerError(f"could not parse funds from {data}")

    # --------------------------------------------------------- positions
    def positions(self) -> list[BrokerPosition]:
        _take(self._generic, 10, "position")
        try:
            resp = self._transport.position()
        except Exception as e:
            raise BrokerError(f"position() failed: {self._scrub(e)}", retryable=True)
        net: dict[str, int] = {}
        cost: dict[str, float] = {}
        for r in self._rows(resp, "position()"):
            sym = self._engine_symbol(r)
            if not sym:
                raise BrokerError(f"position row without a symbol: {r}", retryable=True)
            qty = _int_field(r, "netqty")
            if qty == 0:
                continue
            # one row per product type: net them, never let one overwrite another
            net[sym] = net.get(sym, 0) + qty
            cost[sym] = cost.get(sym, 0.0) + qty * _float_field(r, "netprice")
        return [BrokerPosition(sym, q, cost[sym] / q) for sym, q in net.items() if q != 0]

    def open_orders(self) -> list[BrokerOrder]:
        """Every order in today's book (open or not)."""
        _take(self._generic, 10, "orderBook")
        try:
            resp = self._transport.orderBook()
        except Exception as e:
            raise BrokerError(f"orderBook failed: {self._scrub(e)}", retryable=True)
        return [self._parse_order(r) for r in self._rows(resp, "orderBook")]

    def find_by_tag(self, client_order_id: str) -> BrokerOrder | None:
        """Today's order carrying this ordertag, or None when the book has no
        such order. An unreadable book raises: "not found" and "could not
        look" lead to opposite decisions about resending."""
        for o in self.open_orders():
            if o.client_order_id == client_order_id:
                if o.broker_order_id:
                    self._client_to_broker.setdefault(client_order_id, o.broker_order_id)
                return o
        return None

    def register_order(self, client_order_id: str, broker_order_id: str) -> None:
        """Restore a mapping known from the order journal after a restart."""
        if client_order_id and broker_order_id:
            self._client_to_broker[client_order_id] = broker_order_id
            self._unresolved.discard(client_order_id)

    # ------------------------------------------------------------- orders
    def place(self, order: BrokerOrder) -> OrderAck:
        # idempotency: never resubmit a known client id
        if order.client_order_id in self._client_to_broker:
            boid = self._client_to_broker[order.client_order_id]
            return OrderAck(order.client_order_id, boid, OrderStatus.SUBMITTED,
                            "idempotent: already placed")
        inst = self._instruments.get(order.symbol)
        if inst is None:
            raise BrokerError(f"unknown instrument {order.symbol}")
        # ordertag is the postback correlation key — must survive verbatim.
        # Refuse rather than silently truncate (a truncated tag = lost fills).
        if len(order.client_order_id) > 20:
            raise BrokerError(
                f"client_order_id {order.client_order_id!r} exceeds Angel's "
                "20-char ordertag limit; OMS must emit compact ids")
        if order.style == ExecutionStyle.MARKET_SINGLE:
            ordertype, price = "MARKET", "0"
        else:
            ordertype, price = "LIMIT", _limit_price_text(order, inst)
        params = {
            "variety": "NORMAL",
            "tradingsymbol": inst.broker_symbol or inst.symbol,
            "symboltoken": inst.token,
            "transactiontype": order.side,
            "exchange": inst.exchange,
            "ordertype": ordertype,
            "producttype": _product_type(inst, order),
            "duration": "DAY",
            "quantity": str(order.qty),
            "price": price,
            "ordertag": order.client_order_id,  # postback correlation (<=20 chars)
        }
        cid = order.client_order_id
        if cid in self._unresolved:
            # an earlier send of this id may have reached Angel: look first
            found = self._lookup_twice(cid)
            if found is not None:
                return self._recovered(cid, found, "recovered by ordertag lookup")
            self._unresolved.discard(cid)
        _take(self._orders, 5, "placeOrder")
        self._unresolved.add(cid)  # unknown until a response says otherwise
        try:
            resp = self._transport.placeOrder(params)
        except Exception as e:
            return self._after_unknown_send(
                cid, f"placeOrder failed: {self._scrub(e)}", retryable=True)
        boid = _extract_order_id(resp)
        if not boid:
            return self._after_unknown_send(
                cid, f"no order id in response: {self._scrub(resp)}", retryable=False)
        self._unresolved.discard(cid)
        self._client_to_broker[cid] = boid
        return OrderAck(cid, boid, OrderStatus.SUBMITTED)

    def _after_unknown_send(self, cid: str, why: str, *, retryable: bool) -> OrderAck:
        """The request may have been accepted even though we could not read
        the answer. Two order-book lookups by ordertag decide: found means it
        is live; nothing twice means a resend is allowed; an unreadable book
        leaves the outcome unknown and nothing may send it again."""
        found = self._lookup_twice(cid)
        if found is not None:
            return self._recovered(cid, found, f"recovered by ordertag lookup ({why})")
        self._unresolved.discard(cid)
        raise BrokerError(f"{why}; ordertag {cid} not in the order book after two "
                          "lookups", retryable=retryable)

    def _lookup_twice(self, cid: str) -> BrokerOrder | None:
        for _ in range(2):
            if self.lookup_pause_s > 0:
                time.sleep(self.lookup_pause_s)
            try:
                found = self.find_by_tag(cid)
            except BrokerError as e:
                raise OrderStateUnknown(
                    f"order {cid}: send outcome unknown and the order book is "
                    f"unreadable ({e}); not resending") from e
            if found is not None:
                return found
        return None

    def _recovered(self, cid: str, found: BrokerOrder, detail: str) -> OrderAck:
        self._unresolved.discard(cid)
        if found.broker_order_id:
            self._client_to_broker[cid] = found.broker_order_id
        log.warning("order %s found at the broker as %s (%s): not resent", cid,
                    found.broker_order_id, found.status.value)
        return OrderAck(cid, found.broker_order_id, found.status, detail)

    def cancel(self, client_order_id: str) -> OrderAck:
        """Requests a cancel. The ack only says Angel took the request; the
        order is cancelled once order_status reports it terminal."""
        boid = self._client_to_broker.get(client_order_id, "")
        if not boid:
            raise BrokerError(f"unknown client_order_id {client_order_id}")
        _take(self._orders, 5, "cancelOrder")
        try:
            resp = self._transport.cancelOrder(boid, "NORMAL")
        except Exception as e:
            raise BrokerError(f"cancelOrder failed: {self._scrub(e)}", retryable=True)
        if not isinstance(resp, dict) or resp.get("status") is not True:
            raise BrokerError(f"cancelOrder {boid} refused: {self._scrub(resp)[:300]}")
        return OrderAck(client_order_id, boid, OrderStatus.SUBMITTED, "cancel requested")

    def order_status(self, client_order_id: str) -> BrokerOrder | None:
        boid = self._client_to_broker.get(client_order_id)
        for o in self.open_orders():
            if (boid and o.broker_order_id == boid) or o.client_order_id == client_order_id:
                if o.broker_order_id and not boid:
                    self._client_to_broker[client_order_id] = o.broker_order_id
                return o
        return None

    def _parse_order(self, r: dict) -> BrokerOrder:
        raw = str(r.get("status", "")).strip().lower()
        status = _STATUS_MAP.get(raw)
        if status is None:
            status = OrderStatus.UNKNOWN
            if raw not in self._unknown_statuses:
                self._unknown_statuses.add(raw)
                log.warning("unrecognised Angel order status %r: treated as working "
                            "and never resendable", raw)
        qty = _int_field(r, "quantity")
        filled = _int_field(r, "filledshares")
        # The fill counter, not the status text, says how much traded.
        if status is OrderStatus.SUBMITTED and filled > 0:
            status = OrderStatus.PARTIAL
        elif status is OrderStatus.FILLED:
            if str(r.get("filledshares") or "") == "":
                filled = qty
            elif filled < qty:
                status = OrderStatus.UNKNOWN  # "complete" short of its quantity
        return BrokerOrder(
            client_order_id=r.get("ordertag", ""),
            symbol=self._engine_symbol(r),
            side=r.get("transactiontype", ""),
            qty=qty,
            style=ExecutionStyle.LIMIT_SINGLE,
            urgency=Urgency.NORMAL,
            broker_order_id=str(r.get("orderid", "")),
            status=status,
            filled_qty=filled,
            avg_fill_price=_float_field(r, "averageprice"),
            reject_reason=r.get("text", "") if status == OrderStatus.REJECTED else "",
        )


_MONTHS = {m: i for i, m in enumerate(
    ("JAN", "FEB", "MAR", "APR", "MAY", "JUN",
     "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"), start=1)}


def _parse_expiry(s: str | None) -> date | None:
    """Parse an Angel master expiry like '30JUN2026' (DDMONYYYY, zero-padded)
    into a date. Returns None when absent or unparseable (e.g. a cash-segment
    row with no expiry), so callers can simply skip it."""
    if not s or len(s) != 9:
        return None
    try:
        return date(int(s[5:9]), _MONTHS[s[2:5].upper()], int(s[0:2]))
    except (KeyError, ValueError):
        return None


def _infer_kind(row: dict) -> InstrumentKind:
    seg = (row.get("exch_seg") or "").upper()
    sym = (row.get("symbol") or "").upper()
    # Cash indices (NIFTY 50, NIFTY BANK, ...) carry instrumenttype AMXIDX. They
    # must be INDEX, not EQUITY — they are non-tradeable and their candle data
    # lives under the AMXIDX token, not the bare spot row (see refresh_instruments).
    if (row.get("instrumenttype") or "").upper() in ("AMXIDX", "INDEX"):
        return InstrumentKind.INDEX
    if seg == "NFO":
        if sym.endswith("FUT"):
            return InstrumentKind.FUTURE
        if sym.endswith("CE") or sym.endswith("PE"):
            return InstrumentKind.OPTION
    return InstrumentKind.EQUITY


def _extract_order_id(resp) -> str:
    if isinstance(resp, dict):
        data = resp.get("data") or {}
        return str(data.get("orderid") or resp.get("orderid") or "")
    return str(resp or "")


def _take(bucket: TokenBucket, timeout: float, what: str) -> None:
    """acquire() returns False on timeout. Sending anyway would overrun the
    budget Angel enforces, so the call fails instead (nothing was sent)."""
    if not bucket.acquire(timeout=timeout):
        raise BrokerError(f"{what}: rate limit timeout ({bucket.name} bucket)",
                          retryable=True)


def _int_field(r: dict, key: str) -> int:
    """A whole-number field of a broker row. Blank reads as 0; anything that
    is not a finite whole number raises instead of being truncated."""
    raw = r.get(key)
    if raw is None or raw == "":
        return 0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise BrokerError(f"malformed {key} {raw!r} in broker row", retryable=True) from None
    if not (math.isfinite(value) and value.is_integer()):
        raise BrokerError(f"malformed {key} {raw!r} in broker row", retryable=True)
    return int(value)


def _float_field(r: dict, key: str) -> float:
    raw = r.get(key)
    if raw is None or raw == "":
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise BrokerError(f"malformed {key} {raw!r} in broker row", retryable=True) from None
    if not math.isfinite(value):
        raise BrokerError(f"malformed {key} {raw!r} in broker row", retryable=True)
    return value


def _limit_price_text(order: BrokerOrder, inst: Instrument) -> str:
    px = order.limit_price
    if px is None or not math.isfinite(px) or px <= 0:
        # Angel reads price "0" on a LIMIT as no protection at all
        raise BrokerError(f"refusing LIMIT {order.side} {order.symbol}: price {px!r} "
                          "is not a positive finite number")
    tick = inst.tick_size
    if not (math.isfinite(tick) and tick > 0):
        raise BrokerError(f"refusing LIMIT {order.side} {order.symbol}: tick size "
                          f"{tick!r} is not usable")
    return _price_on_tick(px, tick, round_down=order.side == "BUY")


def _price_on_tick(px: float, tick: float, *, round_down: bool) -> str:
    """Snap to the tick grid in decimal arithmetic and format without float
    noise. Off-grid prices move away from aggression: buys down, sells up."""
    step = Decimal(repr(tick))
    units = Decimal(repr(px)) / step
    nearest = units.to_integral_value(rounding=ROUND_HALF_EVEN)
    if abs(units - nearest) <= _TICK_SNAP:
        units = nearest
    else:
        units = units.to_integral_value(rounding=ROUND_FLOOR if round_down
                                        else ROUND_CEILING)
    return str((units * step).quantize(step))


def _product_type(inst: Instrument, order: BrokerOrder) -> str:
    """Angel product type for how long the engine holds the position."""
    if inst.kind in (InstrumentKind.FUTURE, InstrumentKind.OPTION):
        # held across sessions; INTRADAY is auto squared off before the close
        return "CARRYFORWARD"
    if inst.kind is InstrumentKind.EQUITY:
        if order.side == "BUY" or order.reduce_only:
            return "DELIVERY"
        # a cash-segment short cannot be carried overnight in India
        raise BrokerError(f"refusing cash-equity SELL {order.qty} {order.symbol}: not "
                          "marked reduce-only, so it could open or increase a short")
    raise BrokerError(f"refusing order on {order.symbol}: {inst.kind.value} is not "
                      "tradeable")


# ------------------------------------------------------ SDK containment
_SDK_LOGGERS = ("logzero_default", "SmartApi")
_SDK_LOCK = threading.Lock()


class _SdkOs:
    """Stands in for ``os`` inside SmartApi.smartConnect. Everything passes
    through except makedirs, which its constructor uses only to create
    ./logs/<date>/ for a credential-bearing log file."""

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    @staticmethod
    def makedirs(*args: Any, **kwargs: Any) -> None:
        return None


def _no_logfile(*args: Any, **kwargs: Any) -> None:
    """Replaces logzero.logfile: the SDK modules call it from their
    constructors to attach a file handler to the logger they leak into."""
    return None


def _mute_sdk_logging() -> None:
    """Drop every record from the SDK's loggers for the life of the process,
    whatever handlers it attaches later: its error lines carry the Bearer
    JWT, the api key, the client code, the MPIN and the TOTP."""
    for name in _SDK_LOGGERS:
        lg = logging.getLogger(name)
        for h in list(lg.handlers):
            lg.removeHandler(h)
            h.close()
        lg.propagate = False
        lg.disabled = True
        lg.setLevel(logging.CRITICAL + 1)


def _refused_ip_lookup(*args: Any, **kwargs: Any) -> Any:
    raise OSError("public IP lookup refused while importing SmartApi")


def _smartconnect_class() -> Any:
    """SmartConnect, imported with its side effects contained. At import the
    SDK fetches api.ipify.org with no timeout and then discards the answer
    (its ``finally`` hard-codes the IP), so that request is refused; its
    loggers are muted before anything in it can log."""
    with _SDK_LOCK:
        import logzero

        _mute_sdk_logging()  # logzero attaches its stderr handler at import
        logzero.logfile = _no_logfile
        if "SmartApi.smartConnect" not in sys.modules:
            import requests

            real_get = requests.get
            requests.get = _refused_ip_lookup
            try:
                import SmartApi.smartConnect  # noqa: F401
            finally:
                requests.get = real_get
        from SmartApi import smartConnect as sdk

        sdk.os = _SdkOs()
        return sdk.SmartConnect
