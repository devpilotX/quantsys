"""Order Management System: turns engine OrderIntents into broker orders,
tracks their lifecycle, enforces idempotency, and drives tier-aware execution
algos. Venue-agnostic — works against any Broker (SimBroker in tests,
AngelOneBroker live).

Two rules keep a lost response or a restart from doubling an order:
- every send can go through an OrderJournal, which the live broker uses as a
  write-ahead log (the order row is committed before the broker call);
- a slice whose send outcome is unknown, or that the broker refused, is never
  sent again by this OMS.

Before a new parent goes out for a symbol, the working orders of that
symbol's older parents are cancelled and the cancel confirmed; otherwise the
new parent is skipped for this bar, because the engine sized it from filled
positions only and a resting order could fill on top of it. For the same
reason it is skipped when those orders filled after the position read it was
sized from: the caller runs observe_working() just before that read, so only
fills after it count.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from quantsys.core.types import (
    ExecutionStyle,
    Instrument,
    OrderIntent,
    Urgency,
)
from quantsys.execution.broker import (
    Broker,
    BrokerError,
    BrokerOrder,
    OrderAck,
    OrderStateUnknown,
    OrderStatus,
)

log = logging.getLogger("quantsys.oms")

# Source tag in client ids: a flatten issued in the same minute as a decision
# must not derive the decision's id (the decision's order would be dropped).
SOURCE_DECISION = "D"
SOURCE_FLATTEN = "F"

# Slice lifecycle. Only PENDING slices are ever sent.
PENDING = "pending"
PLACED = "placed"
FAILED = "failed"          # broker refused it: never resent
IN_DOUBT = "in_doubt"      # outcome unknown: never resent, resolve by lookup
ABANDONED = "abandoned"    # superseded by a newer parent before it was sent


@dataclass
class Slice:
    qty: int
    client_order_id: str = ""
    state: str = PENDING
    status: OrderStatus | None = None  # last status seen at the broker
    filled_seen: int = 0               # filled qty at that observation
    error: str = ""                    # why it is FAILED or IN_DOUBT

    @property
    def placed(self) -> bool:
        return self.state == PLACED


@dataclass
class ManagedOrder:
    """A parent intent and its child slices (TWAP/AC) or a single child."""

    parent_id: str
    symbol: str
    side: str
    total_qty: int
    style: ExecutionStyle
    urgency: Urgency
    strategy: str
    slices: list[Slice] = field(default_factory=list)
    created: datetime | None = None
    done: bool = False
    reason: str = ""
    reduce_only: bool = False
    source: str = SOURCE_DECISION
    context: dict[str, Any] = field(default_factory=dict)  # caller data for the journal


class OrderJournal(Protocol):
    """Durable record of every send, consulted before the broker call."""

    def before_send(self, order: BrokerOrder, mo: ManagedOrder) -> OrderAck | None:
        """Record the order before it is sent. Return None to send it, or the
        ack of an order that already exists so it is not sent again. Raise
        OrderStateUnknown when an earlier attempt cannot be resolved; any
        other exception means it could not be recorded, so it is not sent."""
        ...

    def after_send(self, order: BrokerOrder, ack: OrderAck) -> None: ...

    def send_failed(self, order: BrokerOrder, error: BrokerError) -> None: ...

    def status_seen(self, order: BrokerOrder) -> None:
        """A status read from the broker for one of our orders."""
        ...


class NullJournal:
    """No durable record: engine-only use and tests."""

    def before_send(self, order: BrokerOrder, mo: ManagedOrder) -> OrderAck | None:
        return None

    def after_send(self, order: BrokerOrder, ack: OrderAck) -> None:
        return None

    def send_failed(self, order: BrokerOrder, error: BrokerError) -> None:
        return None

    def status_seen(self, order: BrokerOrder) -> None:
        return None


class OMS:
    def __init__(self, broker: Broker, *, max_retries: int = 2,
                 slice_pause_s: float = 1.0, clock=time.monotonic,
                 journal: OrderJournal | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.broker = broker
        self.max_retries = max_retries
        self.slice_pause_s = slice_pause_s
        self._clock = clock
        self._sleep = sleep
        self.journal: OrderJournal = journal if journal is not None else NullJournal()
        self.retry_pause_s = 0.2
        # cancel confirmation: this many order-status polls, this far apart
        self.cancel_polls = 8
        self.cancel_poll_s = 0.25
        self.managed: dict[str, ManagedOrder] = {}
        # idempotency: deterministic client ids per (bar_ts, symbol, intent)
        self._submitted: dict[str, str] = {}  # client_order_id -> broker_order_id
        # (symbol, reason) for intents not sent by the last submit_intents call
        self.last_skipped: list[tuple[str, str]] = []
        # bar minute -> flatten calls made in it (the next one's attempt number)
        self._flatten_calls: dict[str, int] = {}

    # --------------------------------------------------------- id helpers
    @staticmethod
    def client_id(bar_ts: datetime, symbol_index: int, seq: int,
                  source: str = SOURCE_DECISION) -> str:
        """Compact, deterministic id that fits Angel One's 20-char ordertag
        verbatim, slice suffix included (no truncation -> postback
        correlation never breaks). Deterministic in (source, bar minute,
        symbol index, seq) so a retried decision re-derives the SAME id and
        cannot double-submit. For a decision seq is the intent's position;
        for a flatten it is the flatten's attempt within the minute, so an
        operator's retry gets ids of its own. The year keeps ids unique
        across the journal's history and the source tag keeps a same-minute
        flatten from taking a decision's id. Fixed-width fields: one that
        outgrew its width could read as a neighbour's id, so that raises
        instead."""
        if source not in (SOURCE_DECISION, SOURCE_FLATTEN):
            raise ValueError(f"unknown order source {source!r}")
        if not (0 <= symbol_index <= 999 and 0 <= seq <= 99):
            raise ValueError(f"symbol_index {symbol_index} / seq {seq} outside the "
                             "id's fixed widths (3 / 2 digits)")
        stamp = bar_ts.strftime("%y%m%d%H%M")                  # 10
        return f"Q{source}{stamp}{symbol_index:03d}{seq:02d}"  # 17, +"-s9" = 20

    def _flatten_attempt(self, ts: datetime) -> int:
        """This flatten's attempt number within its minute (0, 1, ...). The
        count lives in memory, so after a restart a flatten in the same minute
        re-derives attempt 0 and the journal refuses to send it again."""
        stamp = ts.strftime("%y%m%d%H%M")
        n = self._flatten_calls.get(stamp, 0)
        self._flatten_calls[stamp] = n + 1
        return n

    # ------------------------------------------------------ slice planning
    def _plan_slices(self, intent: OrderIntent, inst: Instrument,
                     adv: float | None) -> list[int]:
        """Split qty into child slices by execution style. Lot-aligned."""
        qty = abs(intent.qty_delta)
        lot = max(1, inst.lot_size)
        if intent.style in (ExecutionStyle.MARKET_SINGLE,
                            ExecutionStyle.LIMIT_SINGLE,
                            ExecutionStyle.LIMIT_SMART):
            return [qty]
        # TWAP / Almgren-Chriss: number of slices grows with size vs ADV
        total_lots = max(1, qty // lot)
        if adv and adv > 0:
            participation = qty / adv
            n = int(min(10, max(2, math.ceil(participation / 0.02))))  # ~2% ADV/slice
        else:
            n = 4
        n = min(n, total_lots)  # never more slices than lots
        if n <= 1:
            return [qty]
        base_lots = total_lots // n
        rem = total_lots - base_lots * n  # spread remainder across the first slices
        slices: list[int] = []
        for i in range(n):
            take = base_lots + (1 if i < rem else 0)
            slices.append(take * lot)
        return slices

    # ------------------------------------------------------------- submit
    def submit_intents(self, intents, instruments: dict[str, Instrument],
                       bar_ts: datetime, prices: dict[str, float], *,
                       positions: Mapping[str, int] | None = None,
                       source: str = SOURCE_DECISION,
                       context: Mapping[str, Any] | None = None) -> list[ManagedOrder]:
        """Place all of a decision's intents. Risk-reducing / kill orders go
        first and as marketable; new risk uses the tier's execution style.

        ``positions`` (signed, per symbol) marks orders that only shrink a
        held position as reduce-only; without it nothing is, and the Angel
        adapter refuses every cash-equity sell. Intents not sent (their
        symbol's older orders could not be cleared) are in last_skipped."""
        ordered = sorted(
            intents,
            key=lambda it: 0 if it.urgency in (Urgency.KILL, Urgency.RISK_REDUCING) else 1,
        )
        sym_index = {s: i for i, s in enumerate(sorted(instruments))}
        attempt = self._flatten_attempt(bar_ts) if source == SOURCE_FLATTEN else None
        self.last_skipped = []
        out: list[ManagedOrder] = []
        for seq, intent in enumerate(ordered):
            inst = instruments.get(intent.symbol)
            if inst is None or intent.qty_delta == 0:
                continue
            mo = self._submit_one(intent, inst, bar_ts, seq if attempt is None else attempt,
                                  sym_index.get(intent.symbol, 99), prices,
                                  positions, source, context)
            if mo is not None:
                out.append(mo)
        return out

    def _submit_one(self, intent: OrderIntent, inst: Instrument,
                    bar_ts: datetime, seq: int, symbol_index: int,
                    prices: dict[str, float],
                    positions: Mapping[str, int] | None = None,
                    source: str = SOURCE_DECISION,
                    context: Mapping[str, Any] | None = None) -> ManagedOrder | None:
        side = "BUY" if intent.qty_delta > 0 else "SELL"
        parent_id = self.client_id(bar_ts, symbol_index, seq, source)
        if parent_id in self.managed:
            log.info("order %s already handled; not resubmitting", parent_id)
            return self.managed[parent_id]  # idempotent: already handled this bar

        skip = self._supersede(intent.symbol, bar_ts)
        if skip is not None:
            log.warning("not sending %s %d %s this bar: %s", side,
                        abs(intent.qty_delta), intent.symbol, skip)
            self.last_skipped.append((intent.symbol, skip))
            return None

        held = positions.get(intent.symbol, 0) if positions is not None else 0
        reduce_only = positions is not None and (
            0 < -intent.qty_delta <= held or 0 < intent.qty_delta <= -held)
        slice_qtys = self._plan_slices(intent, inst, inst.adv)
        mo = ManagedOrder(
            parent_id=parent_id, symbol=intent.symbol, side=side,
            total_qty=abs(intent.qty_delta), style=intent.style,
            urgency=intent.urgency, strategy=intent.strategy,
            slices=[Slice(q) for q in slice_qtys], created=bar_ts,
            reason=intent.reason, reduce_only=reduce_only, source=source,
            context=dict(context or {}),
        )
        self.managed[parent_id] = mo

        # immediate slices for single styles; schedulers place slice 0 now and
        # the rest on subsequent pump() calls
        self._place_slice(mo, 0, inst, prices)
        mo.done = not any(s.state == PENDING for s in mo.slices)
        return mo

    def _limit_for(self, mo: ManagedOrder, inst: Instrument,
                   prices: dict[str, float]) -> float | None:
        if mo.style == ExecutionStyle.MARKET_SINGLE:
            return None
        px = prices.get(mo.symbol)
        if px is None:
            return None
        tick = max(inst.tick_size, 1e-6)
        # cross the spread for urgency, else passive by one tick
        aggressive = mo.urgency in (Urgency.KILL, Urgency.RISK_REDUCING)
        sign = 1 if mo.side == "BUY" else -1
        offset = (1 if aggressive else -1) * sign * tick
        return round((px + offset) / tick) * tick

    def _place_slice(self, mo: ManagedOrder, idx: int, inst: Instrument,
                     prices: dict[str, float]) -> None:
        s = mo.slices[idx]
        if s.state != PENDING:
            return
        s.client_order_id = f"{mo.parent_id}-s{idx}"
        order = BrokerOrder(
            client_order_id=s.client_order_id, symbol=mo.symbol, side=mo.side,
            qty=s.qty, style=mo.style, urgency=mo.urgency,
            limit_price=self._limit_for(mo, inst, prices), strategy=mo.strategy,
            ts=mo.created, reduce_only=mo.reduce_only,
        )
        try:
            existing = self.journal.before_send(order, mo)
        except OrderStateUnknown as e:
            self._stop(mo, s, IN_DOUBT, f"earlier attempt unresolved: {e}")
            log.error("order %s NOT sent: earlier attempt unresolved: %s",
                      s.client_order_id, e)
            return
        except Exception as e:
            self._stop(mo, s, FAILED, f"could not journal it: {e}")  # never send it
            raise
        if existing is not None:
            self._accept(s, existing)
            if s.state != PLACED:
                self._stop(mo, s, s.state, existing.detail)
            log.warning("order %s already recorded (%s): not sent again",
                        s.client_order_id, existing.detail)
            return
        for attempt in range(self.max_retries + 1):
            try:
                ack = self.broker.place(order)
            except OrderStateUnknown as e:
                self._stop(mo, s, IN_DOUBT, str(e))
                self.journal.send_failed(order, e)
                log.error("order %s outcome unknown, NOT resending: %s",
                          s.client_order_id, e)
                return
            except BrokerError as e:
                if not e.retryable or attempt == self.max_retries:
                    self._stop(mo, s, FAILED, str(e))
                    self.journal.send_failed(order, e)
                    log.error("order place failed (%s) — NOT retrying: %s",
                              s.client_order_id, e)
                    return  # fail safe: leave unplaced, never spam the market
                self._sleep(self.retry_pause_s * (attempt + 1))
                continue
            self._accept(s, ack)
            self.journal.after_send(order, ack)
            return

    def _accept(self, s: Slice, ack: OrderAck) -> None:
        s.status = ack.status
        if ack.broker_order_id:
            self._submitted[s.client_order_id] = ack.broker_order_id
            s.state = PLACED
        elif ack.status.terminal:
            s.state = FAILED
        else:
            s.state = IN_DOUBT  # exists at the broker, but we cannot address it

    @staticmethod
    def _stop(mo: ManagedOrder, s: Slice, state: str, why: str) -> None:
        """A slice that failed or went in doubt compromises its parent: the
        remaining slices are abandoned, and the next decision re-sizes."""
        s.state = state
        s.error = why
        for other in mo.slices:
            if other.state == PENDING:
                other.state = ABANDONED
        mo.done = True

    # ------------------------------------------------ working-order control
    def observe_working(self) -> None:
        """Record the broker's status and fill count for every placed slice
        not yet seen terminal. Call it just before reading positions: a fill
        seen here is in that read, so the next order on the symbol, sized
        from it, is only held back for fills after it, and a slice seen
        terminal needs no cancel. One order-book read covers every slice; a
        broker without open_orders is asked per slice. A read that fails
        raises BrokerError."""
        todo = [s for mo in self.managed.values() for s in mo.slices
                if s.state == PLACED and s.client_order_id
                and (s.status is None or not s.status.terminal)]
        if not todo:
            return
        read_book = getattr(self.broker, "open_orders", None)
        if read_book is None:
            for s in todo:
                st = self.broker.order_status(s.client_order_id)
                if st is not None:
                    self._observe(s, st)
            return
        book = read_book()
        by_id = {o.broker_order_id: o for o in book if o.broker_order_id}
        by_tag = {o.client_order_id: o for o in book if o.client_order_id}
        for s in todo:
            boid = self._submitted.get(s.client_order_id)
            st = (by_id.get(boid) if boid else None) or by_tag.get(s.client_order_id)
            if st is not None:
                self._observe(s, st)

    def _supersede(self, symbol: str, ts: datetime) -> str | None:
        """Retire the parents on ``symbol`` created before ``ts`` (earlier
        bars, or the bar a flatten interrupts) before a new parent goes out.
        Returns None when it may go, else why it must wait for the next bar."""
        reasons: list[str] = []
        for mo in list(self.managed.values()):
            if mo.symbol == symbol and (mo.created is None or mo.created < ts):
                reasons.extend(self._retire_parent(mo, fills_block=True))
        return "; ".join(reasons) or None

    def cancel_working(self, symbols: set[str] | None = None) -> dict[str, str]:
        """Cancel every working order (optionally only on ``symbols``) and
        confirm each. Returns symbol -> reason for those not confirmed. Fills
        do not count against a symbol here: the caller reads the book after."""
        out: dict[str, list[str]] = {}
        for mo in list(self.managed.values()):
            if symbols is None or mo.symbol in symbols:
                reasons = self._retire_parent(mo, fills_block=False)
                if reasons:
                    out.setdefault(mo.symbol, []).extend(reasons)
        return {sym: "; ".join(r) for sym, r in out.items()}

    def _retire_parent(self, mo: ManagedOrder, *, fills_block: bool) -> list[str]:
        """Unsent slices are abandoned and working ones cancelled. With
        ``fills_block``, fills since the last observation also block: the
        caller sized its order from a position read that predates them."""
        for s in mo.slices:
            if s.state == PENDING:
                s.state = ABANDONED
        mo.done = True
        reasons = []
        for s in mo.slices:
            if s.state not in (PLACED, IN_DOUBT):
                continue
            seen_before = s.filled_seen
            why = self._retire(s)
            if why is None and fills_block and s.filled_seen != seen_before:
                why = (f"{s.client_order_id}: filled {s.filled_seen - seen_before} more "
                       "since last seen, so this bar's position may predate those fills")
            if why is not None:
                reasons.append(why)
        return reasons

    def _retire(self, s: Slice) -> str | None:
        """Make sure slice ``s`` can no longer trade: None once it is terminal
        (or never reached the broker), else why it may still trade."""
        if s.status is not None and s.status.terminal:
            return None
        cid = s.client_order_id
        try:
            st = self.broker.order_status(cid)
            if st is None and s.state == IN_DOUBT:
                st = self.broker.order_status(cid)  # two lookups before "never sent"
                if st is None:
                    s.state = FAILED
                    log.warning("in-doubt order %s not at the broker after two "
                                "lookups: treating it as never accepted", cid)
                    return None
        except BrokerError as e:
            return f"{cid}: status unreadable ({e})"
        if st is None:
            return f"{cid}: not found at the broker"
        s.state = PLACED
        self._observe(s, st)
        if not st.status.terminal:
            try:
                self.broker.cancel(cid)
            except BrokerError as e:
                return f"{cid}: cancel failed ({e})"
            for _ in range(max(1, self.cancel_polls)):
                self._sleep(self.cancel_poll_s)
                try:
                    polled = self.broker.order_status(cid)
                except BrokerError as e:
                    log.warning("order status %s unreadable: %s", cid, e)
                    continue
                if polled is not None:
                    self._observe(s, polled)
                    if polled.status.terminal:
                        break
            if s.status is None or not s.status.terminal:
                return f"{cid}: cancel not confirmed (status {s.status})"
        return None

    def _observe(self, s: Slice, st: BrokerOrder) -> None:
        s.status = st.status
        s.filled_seen = st.filled_qty
        self.journal.status_seen(st)

    def adopt(self, client_order_id: str, *, symbol: str, side: str, qty: int,
              created: datetime | None, filled_qty: int = 0,
              broker_order_id: str = "", in_doubt: bool = False) -> None:
        """Track an order known from the journal (after a restart) so the next
        parent on its symbol cancels it first. Its parent is not resumed."""
        parent_id = client_order_id.rsplit("-s", 1)[0]
        mo = self.managed.get(parent_id)
        if mo is None:
            mo = ManagedOrder(parent_id=parent_id, symbol=symbol, side=side,
                              total_qty=qty, style=ExecutionStyle.LIMIT_SINGLE,
                              urgency=Urgency.NORMAL, strategy="", created=created,
                              done=True)
            self.managed[parent_id] = mo
        mo.slices.append(Slice(qty, client_order_id=client_order_id,
                               state=IN_DOUBT if in_doubt else PLACED,
                               filled_seen=filled_qty))
        if broker_order_id:
            self._submitted[client_order_id] = broker_order_id

    # ---------------------------------------------------------------- pump
    def pump(self, instruments: dict[str, Instrument],
             prices: dict[str, float]) -> None:
        """Advance scheduled (TWAP/AC) orders and refresh statuses. Call once
        per bar (or sub-bar tick) between decisions."""
        for mo in list(self.managed.values()):
            if mo.done:
                continue
            inst = instruments.get(mo.symbol)
            if inst is None:
                continue
            for idx, s in enumerate(mo.slices):
                if s.state == PENDING:
                    self._place_slice(mo, idx, inst, prices)
                    break  # one slice per pump => time-spreading
            mo.done = mo.done or not any(s.state == PENDING for s in mo.slices)

    def reconcile_statuses(self) -> dict[str, BrokerOrder]:
        out: dict[str, BrokerOrder] = {}
        for mo in self.managed.values():
            for s in mo.slices:
                if s.placed and s.client_order_id:
                    st = self.broker.order_status(s.client_order_id)
                    if st is not None:
                        out[s.client_order_id] = st
        return out
