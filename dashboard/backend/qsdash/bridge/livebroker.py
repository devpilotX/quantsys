"""LiveExecutionBroker — presents the same surface the bridge Runner calls
(execute/flatten_all/positions/equity/...) but routes orders through the real
Angel One adapter + OMS, persists fills/orders/positions to Postgres, and
reconciles against the broker every cycle (freeze on mismatch).

It deliberately mirrors PaperBroker's method names so the Runner is venue-
agnostic; the ONLY behavioural difference is that fills come from the broker
(via postback/order-status), not synthesised at the bar close.

Two records here do not come from a broker read:

- The order journal. The live broker is the OMS's journal: every order row is
  committed as PENDING_SUBMIT before the broker call and moved to SUBMITTED
  with the broker id after it. A row that already exists is resolved (it has
  a broker id, or a lookup by ordertag finds it) instead of being sent again,
  so neither a lost response nor a restart can double an order.
- The intended book. A baseline broker snapshot taken once, when live trading
  first starts (PositionRow mode='live', strategy 'baseline', plus the fill
  watermark in runtime_config), plus the signed sum of live fills recorded
  after it. Reconciliation compares that book with one broker snapshot per
  bar; comparing the broker with a second read of itself could never fail.

A read that fails raises BrokerError: an unreadable broker is never turned
into an empty book, and equity is never computed as MTM only.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from qsdash.bus import PgSyncPublisher, SyncPublisher
from qsdash.db import SessionLocal, now_ist
from qsdash.models import (
    MODE_LIVE,
    FillRow,
    OrderRow,
    PositionRow,
    RiskEvent,
    RuntimeConfig,
)
from quantsys.core.types import (
    Decision,
    ExecutionStyle,
    Instrument,
    InstrumentKind,
    OrderIntent,
    Position,
    Urgency,
)
from quantsys.execution.broker import (
    Broker,
    BrokerError,
    BrokerOrder,
    BrokerPosition,
    OrderAck,
    OrderStateUnknown,
    OrderStatus,
)
from quantsys.execution.oms import OMS, SOURCE_DECISION, SOURCE_FLATTEN, ManagedOrder
from quantsys.execution.reconcile import ReconResult, reconcile_books

log = logging.getLogger("qsdash.livebroker")

PENDING_SUBMIT = "PENDING_SUBMIT"
# order rows that may still trade; their symbols are left out of reconciliation
WORKING_STATUSES = ("NEW", PENDING_SUBMIT, "SUBMITTED", "PARTIAL", "UNKNOWN")
TERMINAL_STATUSES = ("FILLED", "CANCELLED", "REJECTED")

BASELINE_KEY = "live_book_baseline"   # runtime_config: {"ts", "fill_id"}
BASELINE_STRATEGY = "baseline"

FUNDS_MAX_AGE_S = 120.0   # older than this, a failed funds read halts the bar
SNAPSHOT_TTL_S = 30.0     # one snapshot serves every call within a bar
# The order book can show a fill before its postback has booked it. For this
# long after that was seen, the symbol is left out of reconciliation; after
# it, the fill counts as lost and reconciliation reports the symbol.
POSTBACK_GRACE_S = 120.0


@dataclass(frozen=True)
class BookSnapshot:
    """One broker read: positions by engine symbol, and funds."""

    positions: dict[str, BrokerPosition]
    funds: float
    taken: float  # clock() at the read


class LiveExecutionBroker:
    def __init__(self, broker: Broker, instruments: dict[str, Instrument],
                 publisher: SyncPublisher, *,
                 funds_max_age_s: float = FUNDS_MAX_AGE_S,
                 snapshot_ttl_s: float = SNAPSHOT_TTL_S,
                 postback_grace_s: float = POSTBACK_GRACE_S,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.mode = MODE_LIVE
        self.broker = broker
        self.instruments = instruments
        self.publisher = publisher
        self.funds_max_age_s = funds_max_age_s
        self.snapshot_ttl_s = snapshot_ttl_s
        self.postback_grace_s = postback_grace_s
        self.clock = clock
        self.oms = OMS(broker, journal=self, sleep=sleep)
        self._snap: BookSnapshot | None = None
        self._last_funds: tuple[float, float] | None = None  # (value, clock())
        self._baseline: dict[str, int] = {}
        self._watermark = 0      # fills with an id above this count toward the book
        self._loaded = False
        self._last_recon_ok = True
        self._last_mismatch: list[str] = []
        # client id -> (symbol, filled at the broker, clock()) for orders seen
        # finished at the broker with fills no postback has booked yet
        self._unbooked: dict[str, tuple[str, int, float]] = {}

    # ------------------------------------------------------ bar snapshot
    def refresh_snapshot(self) -> BookSnapshot:
        """The bar's read: observe the OMS's working orders, then read
        positions and funds once. Observing first means an order sized from
        these positions is only held back for fills after the read. Raises
        BrokerError when positions are unreadable, or funds are and the last
        good value is older than funds_max_age_s. The first call also loads
        the journal and, if no baseline is persisted yet, takes one."""
        if not self._loaded:
            self.load_open_state()
        try:
            self.oms.observe_working()
        except BrokerError as e:
            log.error("order book unreadable before the position read (%s): an order "
                      "that filled since the last read holds back the next order on "
                      "its symbol for a bar", e)
        return self._read_book()

    def _read_book(self) -> BookSnapshot:
        positions = {p.symbol: p for p in self.broker.positions() if p.qty != 0}
        now = self.clock()
        try:
            funds = self.broker.funds()
            self._last_funds = (funds, now)
        except BrokerError as e:
            if self._last_funds is None or now - self._last_funds[1] > self.funds_max_age_s:
                raise BrokerError(f"funds unreadable ({e}) and no good value younger than "
                                  f"{self.funds_max_age_s:.0f}s", retryable=True) from e
            funds = self._last_funds[0]
            log.warning("funds read failed (%s); using the value from %.0fs ago",
                        e, now - self._last_funds[1])
        self._snap = BookSnapshot(positions, funds, now)
        return self._snap

    def _current_snapshot(self) -> BookSnapshot:
        snap = self._snap
        if snap is None:
            return self.refresh_snapshot()
        if self.clock() - snap.taken > self.snapshot_ttl_s:
            # Re-read without observing: this bar's orders were sized from the
            # bar's read, and a fill marked seen now would stop holding them back.
            return self._read_book()
        return snap

    # --------------------------------------------------------- marking
    def positions_map(self) -> dict[str, int]:
        return {s: p.qty for s, p in self._current_snapshot().positions.items()}

    @property
    def positions(self) -> dict[str, Position]:
        # adapter to the Runner's expectation (symbol -> object with .qty)
        return {s: Position(s, p.qty, p.avg_price)
                for s, p in self._current_snapshot().positions.items()}

    def equity(self, prices: dict[str, float]) -> float:
        snap = self._current_snapshot()
        return snap.funds + self._mtm(snap, prices)

    def _mtm(self, snap: BookSnapshot, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, p in snap.positions.items():
            px = prices.get(sym)
            inst = self.instruments.get(sym)
            if px is not None and inst is not None:
                total += p.qty * px * inst.point_value
        return total

    def mtm(self, prices: dict[str, float]) -> float:
        return self._mtm(self._current_snapshot(), prices)

    def unrealized(self, prices: dict[str, float]) -> float:
        total = 0.0
        for sym, p in self._current_snapshot().positions.items():
            px = prices.get(sym)
            inst = self.instruments.get(sym)
            if px is not None and inst is not None:
                total += p.qty * (px - p.avg_price) * inst.point_value
        return total

    def exposures(self, prices: dict[str, float]) -> tuple[float, float]:
        gross = net = 0.0
        for sym, p in self._current_snapshot().positions.items():
            px = prices.get(sym)
            inst = self.instruments.get(sym)
            if px is None or inst is None:
                continue
            notional = p.qty * px * inst.point_value
            gross += abs(notional)
            net += notional
        return gross, net

    @property
    def cash(self) -> float:
        return self._current_snapshot().funds

    @property
    def realized_total(self) -> float:
        return 0.0  # realized P&L is derived from fills in the DB for live

    # ------------------------------------------------------ restart state
    def load_open_state(self, positions: dict[str, BrokerPosition] | None = None) -> None:
        """Resume after a start or restart: the intended book's baseline
        (taken from ``positions`` or a broker read if none is persisted) and
        the journal's open orders, mapped back into the adapter and the OMS
        so cancel() and order_status() work and the next order on a symbol
        cancels them first."""
        sess = SessionLocal()
        try:
            self._load_baseline(sess, positions)
            self._reload_open_orders(sess)
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()
        self._loaded = True

    def _ensure_loaded(self) -> None:
        if not self._loaded:
            self.load_open_state()

    def _load_baseline(self, sess: Session,
                       positions: dict[str, BrokerPosition] | None) -> None:
        state = sess.query(RuntimeConfig).filter(RuntimeConfig.key == BASELINE_KEY).one_or_none()
        if state is not None:
            v = (state.value or {}).get("v") or {}
            try:
                self._watermark = int(v["fill_id"])
            except (KeyError, TypeError, ValueError) as e:
                raise BrokerError(f"{BASELINE_KEY} is malformed ({v!r}); refusing to "
                                  "trade on an unknown book") from e
            self._baseline = {}
            for row in (sess.query(PositionRow)
                        .filter(PositionRow.mode == self.mode, PositionRow.status == "open",
                                PositionRow.strategy == BASELINE_STRATEGY)):
                self._baseline[row.symbol] = self._baseline.get(row.symbol, 0) + row.qty
            return
        if positions is None:
            positions = {p.symbol: p for p in self.broker.positions() if p.qty != 0}
        self._watermark = (sess.query(func.max(FillRow.id))
                           .filter(FillRow.mode == self.mode).scalar()) or 0
        ts = now_ist()
        for sym, p in positions.items():
            sess.add(PositionRow(
                mode=self.mode, symbol=sym, strategy=BASELINE_STRATEGY, qty=p.qty,
                avg_price=p.avg_price, status="open", opened_at=ts,
                entry_rationale={"note": "broker snapshot when live trading started"}))
        sess.add(RuntimeConfig(key=BASELINE_KEY, updated_by="engine", value={
            "v": {"ts": ts.isoformat(), "fill_id": self._watermark}}))
        self._baseline = {s: p.qty for s, p in positions.items()}
        log.warning("live book baseline taken from the broker: %s (fills above id %d "
                    "count on top)", self._baseline, self._watermark)

    def _reload_open_orders(self, sess: Session) -> None:
        today = now_ist().date()
        rows = (sess.query(OrderRow)
                .filter(OrderRow.mode == self.mode, OrderRow.status.in_(WORKING_STATUSES))
                .order_by(OrderRow.id).all())
        for row in rows:
            if row.ts.date() < today:
                # Angel DAY orders die at the close; that book is gone. A fill
                # we never heard about shows up as a reconciliation mismatch.
                _set_status(row, "CANCELLED", "expired: DAY order from an earlier session")
                continue
            if not row.broker_order_id:
                try:
                    found = self._lookup_twice(row.client_order_id)
                except BrokerError as e:
                    log.error("order %s: send outcome still unknown (%s); its symbol "
                              "stays blocked", row.client_order_id, e)
                    self._adopt(row, in_doubt=True)
                    continue
                if found is None:
                    _set_status(row, "REJECTED",
                                "not in the broker's order book: never accepted")
                    continue
                row.broker_order_id = found.broker_order_id
                _set_status(row, "SUBMITTED", "found by ordertag after a restart")
            self.broker.register_order(row.client_order_id, row.broker_order_id)
            self._adopt(row)

    def _adopt(self, row: OrderRow, *, in_doubt: bool = False) -> None:
        self.oms.adopt(row.client_order_id, symbol=row.symbol, side=row.side, qty=row.qty,
                       created=row.ts, filled_qty=row.filled_qty,
                       broker_order_id=row.broker_order_id, in_doubt=in_doubt)

    def _lookup_twice(self, cid: str) -> BrokerOrder | None:
        """None only when two readable order-book lookups agree it is absent;
        an unreadable book raises BrokerError."""
        for _ in range(2):
            found = self.broker.find_by_tag(cid)
            if found is not None:
                return found
        return None

    def rebaseline(self) -> dict[str, int]:
        """Operator action, after a reconciliation halt has been explained:
        retire the old baseline and take a new one from the broker now."""
        positions = {p.symbol: p for p in self.broker.positions() if p.qty != 0}
        sess = SessionLocal()
        try:
            ts = now_ist()
            for row in (sess.query(PositionRow)
                        .filter(PositionRow.mode == self.mode, PositionRow.status == "open",
                                PositionRow.strategy == BASELINE_STRATEGY)):
                row.status = "closed"
                row.closed_at = ts
            sess.query(RuntimeConfig).filter(RuntimeConfig.key == BASELINE_KEY).delete()
            self._load_baseline(sess, positions)
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()
        self.record_risk_event("live_book_rebaselined", f"new baseline {self._baseline}",
                               severity="warn")
        return dict(self._baseline)

    # ------------------------------------------------------ reconciliation
    def intended_book(self) -> dict[str, int]:
        """Baseline plus every live fill recorded after it, per symbol."""
        self._ensure_loaded()
        book = dict(self._baseline)
        sess = SessionLocal()
        try:
            rows = (sess.query(FillRow.symbol, func.sum(FillRow.qty))
                    .filter(FillRow.mode == self.mode, FillRow.id > self._watermark)
                    .group_by(FillRow.symbol).all())
        finally:
            sess.close()
        for sym, qty in rows:
            book[sym] = book.get(sym, 0) + int(qty)
        return {s: q for s, q in book.items() if q != 0}

    def working_symbols(self) -> set[str]:
        """Symbols whose fills may be in flight: orders still working, and
        orders seen finished at the broker whose fills no postback has booked
        yet, for postback_grace_s after that was seen."""
        sess = SessionLocal()
        try:
            rows = (sess.query(OrderRow.symbol)
                    .filter(OrderRow.mode == self.mode,
                            OrderRow.status.in_(WORKING_STATUSES))
                    .distinct().all())
            unbooked = self._unbooked_symbols(sess)
        finally:
            sess.close()
        return {r[0] for r in rows} | unbooked

    def _unbooked_symbols(self, sess: Session) -> set[str]:
        if not self._unbooked:
            return set()
        booked = dict(sess.query(OrderRow.client_order_id, OrderRow.filled_qty)
                      .filter(OrderRow.client_order_id.in_(list(self._unbooked))).all())
        now = self.clock()
        out: set[str] = set()
        for cid, (sym, filled, seen) in list(self._unbooked.items()):
            if booked.get(cid, 0) >= filled:
                del self._unbooked[cid]
            elif now - seen > self.postback_grace_s:
                del self._unbooked[cid]
                log.error("order %s: the broker showed %d filled %.0fs ago and no postback "
                          "has booked it; %s is reconciled from now on", cid, filled,
                          now - seen, sym)
            else:
                out.add(sym)
        return out

    def reconcile(self, snapshot: BookSnapshot | None = None) -> ReconResult:
        """Intended book vs ``snapshot`` (default: this bar's), leaving out
        symbols whose fills may be in flight (working_symbols). Pass the
        returned maps to RiskEngine.reconcile so it sees the same mismatch."""
        snap = snapshot if snapshot is not None else self._current_snapshot()
        broker = {s: p.qty for s, p in snap.positions.items()}
        r = reconcile_books(self.intended_book(), broker, skip=self.working_symbols())
        if r.skipped:
            log.info("reconciliation skipped %s: fills may be in flight", r.skipped)
        if not r.ok:
            log.error("reconciliation mismatch: %s", "; ".join(r.mismatches))
        self._last_recon_ok = r.ok
        self._last_mismatch = r.mismatches
        return r

    # ------------------------------------------------------------ execution
    def execute(self, decision: Decision, decision_id: int | None,
                prices: dict[str, float], ts: datetime) -> None:
        """Route the decision's intents through the OMS. Every order row is
        journaled before its broker call; fills arrive asynchronously via the
        postback webhook (qsdash.api.webhooks) which updates rows + publishes.

        A kill is not sent as the engine's deltas. They were sized from the
        bar's read, and a working order that fills after it would make them
        oversell into a short; the kill flattens the way flatten_all does."""
        if decision.kill_reason is not None:
            self._flatten(prices, ts, reason=f"kill_switch: {decision.kill_reason}",
                          strategy="", source=SOURCE_DECISION, decision_id=decision_id)
            return
        if not decision.orders:
            return
        held = self.positions_map()
        intents = self._refuse_equity_shorts(decision.orders, held, ts)
        self.oms.submit_intents(intents, self.instruments, ts, prices, positions=held,
                                source=SOURCE_DECISION,
                                context={"decision_id": decision_id, "prices": prices})
        self._flag_skipped(ts)

    def _refuse_equity_shorts(self, intents, held: dict[str, int],
                              ts: datetime) -> list[OrderIntent]:
        """A cash-equity sell may only reduce a held long: a cash-segment
        short cannot be carried overnight in India. The part of a sell beyond
        the long is refused (and recorded); the reducing part still goes."""
        out: list[OrderIntent] = []
        for it in intents:
            inst = self.instruments.get(it.symbol)
            if inst is None or inst.kind != InstrumentKind.EQUITY or it.qty_delta >= 0:
                out.append(it)
                continue
            have = max(held.get(it.symbol, 0), 0)
            sell = min(-it.qty_delta, have)
            if sell < -it.qty_delta:
                self.record_risk_event(
                    "equity_short_refused",
                    f"{it.symbol}: asked to sell {-it.qty_delta} holding {have}; the "
                    f"{-it.qty_delta - sell} beyond the long would be a cash-segment "
                    "short, which cannot be carried overnight",
                    symbol=it.symbol, ts=ts,
                    detail={"qty_delta": it.qty_delta, "held": have, "sent": -sell})
            if sell > 0:
                out.append(replace(it, qty_delta=-sell))
        return out

    def _flag_skipped(self, ts: datetime) -> None:
        for sym, why in self.oms.last_skipped:
            self.record_risk_event("order_skipped", why, symbol=sym, ts=ts)

    def pump(self, prices: dict[str, float]) -> None:
        """Advance scheduled slices (TWAP/AC) between bars."""
        try:
            self.oms.pump(self.instruments, prices)
        except BrokerError as e:
            log.error("oms pump failed: %s", e)

    def flatten_all(self, prices: dict[str, float], ts: datetime,
                    reason: str = "operator flatten") -> dict:
        """Close every broker position at market. Working orders are
        cancelled and confirmed first, then the book is read fresh, so the
        exits size against what is really held. A symbol whose working order
        cannot be confirmed cancelled is not flattened: that order could
        still fill on top of the exit."""
        return self._flatten(prices, ts, reason=reason, strategy="operator",
                             source=SOURCE_FLATTEN, decision_id=None)

    def _flatten(self, prices: dict[str, float], ts: datetime, *, reason: str,
                 strategy: str, source: str, decision_id: int | None) -> dict:
        """Returns the symbols whose exit was placed ("sent") and every symbol
        left open with why ("blocked"): a working order not confirmed
        cancelled, a holding outside the universe, or an exit that was not
        placed (its slice state and the broker's reason)."""
        self._ensure_loaded()
        blocked = self.oms.cancel_working()
        snap = self.refresh_snapshot()
        intents = []
        for sym in sorted(snap.positions):
            p = snap.positions[sym]
            if sym in blocked:
                continue
            if sym not in self.instruments:
                log.error("flatten_all: %s is held but not in the engine universe; "
                          "close it at the broker", sym)
                blocked[sym] = "not in the engine universe"
                continue
            intents.append(OrderIntent(
                symbol=sym, qty_delta=-p.qty, style=ExecutionStyle.MARKET_SINGLE,
                urgency=Urgency.KILL, strategy=strategy, reason=reason,
            ))
        sent: list[str] = []
        if intents:
            mos = self.oms.submit_intents(
                intents, self.instruments, ts, prices,
                positions={s: p.qty for s, p in snap.positions.items()},
                source=source, context={"decision_id": decision_id, "prices": prices})
            by_symbol = {mo.symbol: mo for mo in mos}
            skipped = dict(self.oms.last_skipped)
            for it in intents:
                mo = by_symbol.get(it.symbol)
                if mo is None:
                    blocked[it.symbol] = "exit not sent: " + skipped.get(it.symbol, "skipped")
                elif any(s.placed for s in mo.slices):
                    sent.append(it.symbol)
                else:
                    blocked[it.symbol] = "exit not placed: " + "; ".join(
                        f"{s.client_order_id} {s.state}" + (f" ({s.error})" if s.error else "")
                        for s in mo.slices)
            log.warning("flatten_all submitted %d market exits (%s)", len(sent), reason)
        for sym, why in sorted(blocked.items()):
            self.record_risk_event("flatten_blocked", f"{sym}: {why}",
                                   severity="crit", symbol=sym, ts=ts)
        return {"sent": sent, "blocked": blocked}

    # ------------------------------------------------- OrderJournal hooks
    def before_send(self, order: BrokerOrder, mo: ManagedOrder) -> OrderAck | None:
        """Commit the order row before the broker call. An existing row means
        this order was already handled: resolve it rather than send again."""
        ctx = mo.context
        prices = ctx.get("prices") or {}
        sess = SessionLocal()
        self._bind(sess)
        try:
            sess.add(OrderRow(
                client_order_id=order.client_order_id, decision_id=ctx.get("decision_id"),
                ts=mo.created or now_ist(), mode=self.mode, strategy=mo.strategy,
                symbol=mo.symbol, side=mo.side, qty=order.qty, style=mo.style.value,
                urgency=mo.urgency.value, limit_price=order.limit_price,
                ref_price=prices.get(mo.symbol, 0.0), status=PENDING_SUBMIT,
                reason=mo.reason or f"live {mo.style.value}",
                status_history=[{"ts": now_ist().isoformat(), "status": PENDING_SUBMIT,
                                 "via": "journal"}],
            ))
            try:
                sess.flush()
            except IntegrityError as dup:
                sess.rollback()
                return self._resolve_existing(sess, order, dup)
            self.publisher.publish("orders", _order_event(order, self.mode, PENDING_SUBMIT))
            sess.commit()
            return None
        finally:
            self._unbind()
            sess.close()

    def _resolve_existing(self, sess: Session, order: BrokerOrder,
                          dup: IntegrityError) -> OrderAck | None:
        cid = order.client_order_id
        row = sess.query(OrderRow).filter(OrderRow.client_order_id == cid).one_or_none()
        if row is None:
            raise dup  # not a duplicate id: some other constraint failed
        if row.broker_order_id:
            self.broker.register_order(cid, row.broker_order_id)
            return OrderAck(cid, row.broker_order_id, _order_status(row.status),
                            "journal: already sent")
        if row.status in TERMINAL_STATUSES:
            return OrderAck(cid, "", OrderStatus.REJECTED, f"journal: already {row.status}")
        try:
            found = self._lookup_twice(cid)
        except BrokerError as e:
            raise OrderStateUnknown(f"journal row {cid} is {row.status} and the order "
                                    f"book is unreadable ({e})") from e
        if found is None:
            log.warning("journal row %s is %s but not at the broker after two lookups; "
                        "sending it now", cid, row.status)
            return None
        row.broker_order_id = found.broker_order_id
        _set_status(row, "SUBMITTED", "found by ordertag")
        sess.commit()
        self.broker.register_order(cid, found.broker_order_id)
        return OrderAck(cid, found.broker_order_id, found.status, "journal: found by ordertag")

    def after_send(self, order: BrokerOrder, ack: OrderAck) -> None:
        """The order is at the broker now; a failed update here leaves the row
        PENDING_SUBMIT, which a restart resolves by ordertag. Both writes are
        conditional in SQL: a postback that commits between the read below
        and these writes keeps its status and its broker id."""
        cid = order.client_order_id
        sess = SessionLocal()
        self._bind(sess)
        try:
            row = sess.query(OrderRow).filter(OrderRow.client_order_id == cid).one_or_none()
            if row is None:
                log.error("journal row for %s is missing after its send", cid)
                return
            now = now_ist()
            history = [*(row.status_history or []),
                       {"ts": now.isoformat(), "status": "SUBMITTED", "via": "oms",
                        "note": ack.detail or "sent"}]
            (sess.query(OrderRow)
             .filter(OrderRow.client_order_id == cid, OrderRow.broker_order_id == "")
             .update({OrderRow.broker_order_id: ack.broker_order_id},
                     synchronize_session=False))
            moved = (sess.query(OrderRow)
                     .filter(OrderRow.client_order_id == cid,
                             OrderRow.status == PENDING_SUBMIT)
                     .update({OrderRow.status: "SUBMITTED", OrderRow.status_history: history,
                              OrderRow.updated_at: now}, synchronize_session=False))
            status = "SUBMITTED" if moved else (
                sess.query(OrderRow.status).filter(OrderRow.client_order_id == cid).scalar())
            self.publisher.publish("orders", _order_event(order, self.mode, status,
                                                          ack.broker_order_id))
            sess.commit()
        except SQLAlchemyError:
            sess.rollback()
            log.exception("journal update after sending %s failed; the row stays "
                          "PENDING_SUBMIT until a restart resolves it", order.client_order_id)
        finally:
            self._unbind()
            sess.close()

    def send_failed(self, order: BrokerOrder, error: BrokerError) -> None:
        """Refused: the row is REJECTED. Outcome unknown: the row stays
        PENDING_SUBMIT (its symbol stays blocked) and a risk event says so."""
        unknown = isinstance(error, OrderStateUnknown)
        sess = SessionLocal()
        try:
            row = (sess.query(OrderRow)
                   .filter(OrderRow.client_order_id == order.client_order_id).one_or_none())
            if row is not None and row.status == PENDING_SUBMIT:
                if unknown:
                    _set_status(row, PENDING_SUBMIT, f"send outcome unknown: {error}",
                                via="oms")
                else:
                    _set_status(row, "REJECTED", str(error), via="oms")
            sess.commit()
        except SQLAlchemyError:
            sess.rollback()
            log.exception("journal update after the failed send of %s failed",
                          order.client_order_id)
        finally:
            sess.close()
        if unknown:
            self.record_risk_event("order_state_unknown", str(error), severity="crit",
                                   symbol=order.symbol)

    def status_seen(self, order: BrokerOrder) -> None:
        """A terminal status read from the broker closes the row, so a lost
        postback cannot leave its symbol out of reconciliation for good.
        Fills stay the postback's job: filled_qty is not touched here. Fills
        the postbacks have not booked yet keep the symbol out of
        reconciliation for postback_grace_s (working_symbols)."""
        if not order.status.terminal or not order.client_order_id:
            return
        sess = SessionLocal()
        try:
            row = (sess.query(OrderRow)
                   .filter(OrderRow.client_order_id == order.client_order_id).one_or_none())
            if row is not None and row.filled_qty < order.filled_qty:
                self._unbooked.setdefault(order.client_order_id,
                                          (row.symbol, order.filled_qty, self.clock()))
            if row is not None and row.status in WORKING_STATUSES:
                _set_status(row, order.status.value,
                            f"seen at the broker, filled {order.filled_qty}", via="poll")
                sess.commit()
        except SQLAlchemyError:
            sess.rollback()
            log.exception("journal status update for %s failed", order.client_order_id)
        finally:
            sess.close()

    # ------------------------------------------------------------- events
    def record_risk_event(self, kind: str, cause: str, *, severity: str = "warn",
                          symbol: str | None = None, ts: datetime | None = None,
                          detail: dict | None = None) -> None:
        sess = SessionLocal()
        self._bind(sess)
        try:
            when = ts or now_ist()
            sess.add(RiskEvent(ts=when, mode=self.mode, kind=kind, rule=kind,
                               symbol=symbol, severity=severity, cause=cause,
                               detail=detail or {}))
            self.publisher.publish("risk", {"ts": when.isoformat(), "kind": kind,
                                            "symbol": symbol, "cause": cause,
                                            "severity": severity, "mode": self.mode})
            sess.commit()
        except SQLAlchemyError:
            sess.rollback()
            log.exception("risk event %s could not be recorded: %s", kind, cause)
        finally:
            self._unbind()
            sess.close()

    def _bind(self, sess: Session) -> None:
        if isinstance(self.publisher, PgSyncPublisher):
            self.publisher.bind(sess)

    def _unbind(self) -> None:
        if isinstance(self.publisher, PgSyncPublisher):
            self.publisher.bind(None)


def _set_status(row: OrderRow, status: str, note: str, via: str = "journal") -> None:
    hist = list(row.status_history or [])
    hist.append({"ts": now_ist().isoformat(), "status": status, "via": via, "note": note})
    row.status_history = hist
    row.status = status
    row.updated_at = now_ist()
    if status == "REJECTED":
        row.reject_reason = note


def _order_status(text: str) -> OrderStatus:
    if text == PENDING_SUBMIT:
        return OrderStatus.SUBMITTED
    try:
        return OrderStatus(text)
    except ValueError:
        return OrderStatus.UNKNOWN


def _order_event(order: BrokerOrder, mode: str, status: str,
                 broker_order_id: str = "") -> dict:
    return {"client_order_id": order.client_order_id, "symbol": order.symbol,
            "side": order.side, "qty": order.qty, "status": status, "mode": mode,
            "broker_order_id": broker_order_id}
