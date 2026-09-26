"""Calendar-structural expiry effect (Phase-4 research candidate, ONE
pre-registered hypothesis — NOT to be tuned).

NSE monthly F&O settlement clusters in the last week of the calendar month. Into
that window, options open interest concentrates and market-maker delta-hedging +
cash/derivative settlement flows tend to PIN the underlying and mean-revert sharp
short-horizon deviations, which then unwind. Hypothesis: during the month-end
settlement window, FADE deviations of price from its recent rolling mean in
liquid names; stay flat otherwise.

The window is the last `window_days` calendar days of the month. The code never
computes an expiry date, and that calendar window is the registered rule. NSE
moved monthly F&O expiry from the last Thursday of the month to the last
Tuesday on 1 September 2025 (the Jun-2026 contract expires Tue 30-Jun). The
last seven calendar days of a month hold each weekday once, so with
window_days >= 7 the window always contains the expiry day, but where the
expiry sits in it depends on the weekday the month ends on. Under
Tuesday expiry a month ending Friday, Saturday, Sunday or Monday puts three or
four of the window's five weekday sessions after expiry (Aug-2026: expiry
Tue 25, window Tue 25 to Mon 31, four sessions after it), so in those months
the sleeve mostly fades deviations after expiry, not into it. With
window_days < 7 the expiry day can fall outside the window. Exchange holidays
are not modelled: they remove sessions from the window and can move the expiry
day, never the window.

Defined-risk: an ATR stop on every position. Declarative targets — a signal is
emitted every decision bar while a position is wanted; ceasing to emit IS the
exit instruction (identical contract to trend/meanrev).
"""

from __future__ import annotations

import calendar
import math

import numpy as np

from quantsys.config.schema import ExpiryConfig
from quantsys.core.market_state import MarketState
from quantsys.core.types import InstrumentKind, Signal
from quantsys.data.features import atr
from quantsys.strategies.base import Strategy, register

_TRADEABLE = {InstrumentKind.EQUITY, InstrumentKind.FUTURE}


@register("expiry")
class ExpiryStrategy(Strategy):
    def __init__(self, cfg: ExpiryConfig):
        super().__init__("expiry")
        self.cfg = cfg

    def warmup_bars(self) -> int:
        return self.cfg.timeframe_bars * (self.cfg.z_lookback + 5)

    def _in_expiry_window(self, ts) -> bool:
        """True in the last `window_days` calendar days of the month. A pure
        calendar test: the expiry date is never computed (module docstring)."""
        last_day = calendar.monthrange(ts.year, ts.month)[1]
        return (last_day - ts.day) < self.cfg.window_days

    def generate_signals(self, state: MarketState) -> list[Signal]:
        cfg = self.cfg
        if not self._in_expiry_window(state.ts):
            return []
        out: list[Signal] = []
        need = cfg.z_lookback + 2
        for sym in sorted(state.bars):
            inst = state.instruments.get(sym)
            if inst is None or inst.kind not in _TRADEABLE:
                continue
            rs = state.bars[sym].resampled(cfg.timeframe_bars)
            if not rs or len(rs["close"]) < need:
                continue
            c, h, lo = rs["close"], rs["high"], rs["low"]
            win = c[-cfg.z_lookback:]
            mu, sd = float(np.mean(win)), float(np.std(win))
            if not (sd > 0 and math.isfinite(sd)):
                continue
            z = (c[-1] - mu) / sd
            if abs(z) < cfg.z_entry:
                continue
            a = atr(h, lo, c, cfg.atr_n)
            if not (math.isfinite(a) and a > 0):
                continue
            out.append(
                Signal(
                    strategy=self.name,
                    symbol=sym,
                    direction=-1.0 if z > 0 else 1.0,   # FADE the deviation
                    stop_distance=cfg.atr_mult * a,
                    expected_edge_R=cfg.expected_edge_R,
                    tag=sym,
                )
            )
        return out
