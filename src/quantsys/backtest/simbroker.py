"""SimBroker — in-memory execution venue for backtests.

Fill model (documented assumptions):
- Order intents fill in full at the decision bar's close price. The engine's
  CostModel charges half-spread slippage + square-root impact as explicit cost
  lines, so using close as the fill price does not double-count microstructure
  costs. Impact is only charged when a sigma is supplied, which is why the
  sigma the engine used is read off Decision.sigma_daily rather than omitted.
- Equity equation holds exactly at every bar: equity == cash + MTM. The loop
  asserts it.

Known limits of this model, so no reader mistakes it for fill realism: size is
never capped against the bar's volume or the instrument's ADV, there is no
spread cross beyond the flat per-kind slippage_bps, and a gap bar is filled at
its close like any other. Orders therefore always fill completely, which
flatters any result that depends on being able to trade size.

This is the canonical simulation accounting. The dashboard's PaperBroker
performs the same average-price bookkeeping but persists rows; keep the two
in behavioural lockstep (test_backtest.py cross-checks the identity).

A fill needs a bar: when execute() is told each symbol's last bar time, an
order for a symbol that did not print at the decision bar is not filled,
because its last close predates the decision (see execute()).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime

from quantsys.core.types import Decision, Instrument, InstrumentKind, Position
from quantsys.costs import CostModel

log = logging.getLogger("quantsys.backtest.simbroker")


@dataclass
class ClosedTrade:
    """One round trip: from flat (or a flip) back to flat (or the next flip).

    Adds and partial closes inside the round trip are folded in, so trade
    statistics count round trips rather than fills. qty is the signed total
    opened (initial fill plus adds); entry_price and exit_price are the
    quantity-weighted averages of the opening and closing fills, so
    pnl == qty * (exit_price - entry_price) * point_value.
    """

    symbol: str
    strategy: str
    entry_ts: datetime
    exit_ts: datetime
    qty: int                 # signed total opened over the round trip
    entry_price: float
    exit_price: float
    pnl: float               # realized, gross of fees
    fees: float              # total fees paid over the round trip


@dataclass
class _OpenLot:
    strategy: str
    entry_ts: datetime
    opened: int              # signed quantity opened so far
    entry_value: float       # sum of |qty| * price over the opening fills
    fees: float = 0.0
    realized: float = 0.0
    closed: int = 0          # unsigned quantity closed so far
    exit_value: float = 0.0  # sum of |qty| * price over the closing fills


@dataclass
class SimBroker:
    starting_cash: float
    instruments: dict[str, Instrument]
    cost_model: CostModel

    cash: float = field(init=False)
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[ClosedTrade] = field(default_factory=list)
    total_fees: float = 0.0
    traded_notional: float = 0.0
    n_fills: int = 0
    n_unfilled_no_bar: int = 0
    _lots: dict[str, _OpenLot] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.cash = self.starting_cash

    # ------------------------------------------------------------- marking
    def _notional(self, sym: str, pos: Position, prices: Mapping[str, float]) -> float:
        # Skipping an unmarked position used to value it at zero, which moved
        # equity by its whole notional and could trip the drawdown kill.
        px = prices.get(sym)
        inst = self.instruments.get(sym)
        if px is None or inst is None:
            raise RuntimeError(
                f"cannot value held position {sym} qty={pos.qty}: "
                f"{'no mark' if px is None else 'unknown instrument'}")
        return pos.qty * px * inst.point_value

    def mtm(self, prices: dict[str, float]) -> float:
        return sum((self._notional(s, p, prices) for s, p in self.positions.items()), 0.0)

    def equity(self, prices: dict[str, float]) -> float:
        return self.cash + self.mtm(prices)

    def exposures(self, prices: dict[str, float]) -> tuple[float, float]:
        gross = net = 0.0
        for sym, pos in self.positions.items():
            notional = self._notional(sym, pos, prices)
            gross += abs(notional)
            net += notional
        return gross, net

    # ------------------------------------------------------------ execution
    def execute(self, decision: Decision, prices: dict[str, float],
                ts: datetime, bar_ts: Mapping[str, datetime] | None = None) -> None:
        """Fill the decision's orders at `prices`.

        bar_ts maps each symbol to the time of its latest bar. When given, an
        order for a symbol with no bar at `ts` is not filled and is counted in
        n_unfilled_no_bar: its price is a close from before the decision, and
        filling there trades on a quote the market has already left. Kill
        flattens are held back the same way; the engine re-issues them on the
        next bar because the position is still open. Without bar_ts the
        caller vouches that every price in `prices` is a print at `ts`.
        """
        for intent in decision.orders:
            qty = intent.qty_delta
            sym = intent.symbol
            inst = self.instruments.get(sym)
            if qty == 0 or inst is None:
                continue
            if bar_ts is not None and bar_ts.get(sym) != ts:
                self.n_unfilled_no_bar += 1
                log.debug("no fill for %s %+d at %s: last bar at %s (%s)",
                          sym, qty, ts, bar_ts.get(sym), intent.urgency.value)
                continue
            px = prices.get(sym)
            if px is None or not px > 0:
                continue
            cb = self.cost_model.order_cost(
                inst, abs(qty), px, qty > 0,
                delivery=(inst.kind == InstrumentKind.EQUITY),
                sigma_daily=decision.sigma_daily.get(sym),
            )
            self._apply(sym, intent.strategy, qty, px, cb.total, inst, ts)
            self.cash -= qty * px * inst.point_value
            self.cash -= cb.total
            self.total_fees += cb.total
            self.traded_notional += abs(qty) * px * inst.point_value
            self.n_fills += 1

    def _apply(self, sym: str, strategy: str, qty: int, px: float,
               fees: float, inst: Instrument, ts: datetime) -> None:
        """Position and trade-record bookkeeping for one fill. Cash is moved
        by execute(); nothing here touches it."""
        pos = self.positions.get(sym)
        if pos is None or pos.qty == 0:
            self.positions[sym] = Position(sym, qty, px)
            self._lots[sym] = _OpenLot(strategy or "", ts, qty, abs(qty) * px, fees)
            return

        lot = self._lots.get(sym)
        if lot is None:  # position loaded without a lot: open one at its cost basis
            lot = _OpenLot(strategy or "", ts, pos.qty, abs(pos.qty) * pos.avg_price)
            self._lots[sym] = lot
        if (pos.qty > 0) == (qty > 0):  # add
            new_qty = pos.qty + qty
            pos.avg_price = (pos.avg_price * abs(pos.qty) + px * abs(qty)) / abs(new_qty)
            pos.qty = new_qty
            lot.opened += qty
            lot.entry_value += abs(qty) * px
            lot.fees += fees
            return

        was_long = pos.qty > 0
        closing = min(abs(qty), abs(pos.qty))
        realized = closing * (px - pos.avg_price) * inst.point_value * (1 if was_long else -1)
        remaining = pos.qty + qty
        # A fill that flips through zero ends one round trip and opens the
        # next, so its fee is split by quantity between the two.
        close_fees = fees * closing / abs(qty)
        lot.realized += realized
        lot.fees += close_fees
        lot.closed += closing
        lot.exit_value += closing * px

        if remaining == 0 or (remaining > 0) != was_long:
            self.trades.append(ClosedTrade(
                symbol=sym, strategy=lot.strategy, entry_ts=lot.entry_ts,
                exit_ts=ts, qty=lot.opened,
                entry_price=lot.entry_value / abs(lot.opened),
                exit_price=lot.exit_value / lot.closed,
                pnl=lot.realized, fees=lot.fees,
            ))
            if remaining == 0:
                self.positions.pop(sym, None)
                self._lots.pop(sym, None)
            else:  # flipped through zero
                self.positions[sym] = Position(sym, remaining, px)
                self._lots[sym] = _OpenLot(strategy or lot.strategy, ts, remaining,
                                           abs(remaining) * px, fees - close_fees)
        else:  # partial close: P&L and fees stay on the lot until the round trip ends
            pos.qty = remaining
