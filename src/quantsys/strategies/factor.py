"""Pillar 2 — broad-universe cross-sectional equity factors (momentum + low-vol)
running on an internal DAILY panel.

The original expression resampled ~13 months of intraday decision bars, which a
freshly started live engine cannot have — "enabled" silently meant "wait a
year". This version keeps its own per-symbol daily OHLC panel instead:

  * seeded once at startup from broker daily candles (LiveRunner does this for
    every EQUITY instrument via ``seed_daily``);
  * extended live by aggregating the decision bars it sees into a running daily
    row, finalized whenever the session date rolls.

Cadence is ``rebalance_bars`` SESSION days (~monthly at 21). Composite score is
z(12-1 momentum) + z(low-vol); top-K long, bottom-K short when market_neutral.
Declarative targets: a signal is emitted every bar while a name is wanted;
ceasing to emit IS the exit (identical contract to trend/meanrev). Stops are
daily-ATR based, from the same panel.

Honesty notes: EQUITY-only by design (an index future inside a cross-sectional
stock ranking is a category error); still gated by ``min_universe`` breadth, so
it remains a safe no-op on narrow books or when unseeded — including in the
backtest, which never seeds it. Forward paper study parameters are frozen in
docs/FORWARD_STUDY.md.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import date, timedelta

import numpy as np

from quantsys.config.schema import FactorConfig
from quantsys.core.market_state import MarketState
from quantsys.core.types import InstrumentKind, Signal
from quantsys.strategies._dailypanel import sessions_between
from quantsys.strategies.base import Strategy, register


@register("factor")
class FactorStrategy(Strategy):
    def __init__(self, cfg: FactorConfig):
        super().__init__("factor")
        self.cfg = cfg
        self._depth = (max(cfg.lookback_bars + cfg.skip_bars, cfg.vol_lookback)
                       + cfg.atr_n + 10)
        self._panel: dict[str, deque] = {}   # sym -> deque[(iso_date, high, low, close)]
        self._today: dict[str, list] = {}    # sym -> [iso_date, high, low, close]
        self._dir: dict[str, float] = {}     # sym -> held direction (+1 / -1)
        self._stops: dict[str, float] = {}   # sym -> stop distance (daily-ATR based)
        self._days_since = 10 ** 9           # session days since last rebalance
        self._cur_date: str | None = None

    # ------------------------------------------------------------- seeding
    def seed_daily(self, symbol: str, rows) -> None:
        """Load daily history: rows of (ts_or_date, open, high, low, close, vol)
        ascending — the broker candle tuple shape. Replaces any prior seed."""
        dq: deque[tuple[str, float, float, float]] = deque(maxlen=self._depth)
        for r in rows:
            ts = r[0]
            d = ts.date().isoformat() if hasattr(ts, "date") else str(ts)[:10]
            h, lo, c = float(r[2]), float(r[3]), float(r[4])
            if math.isfinite(c) and c > 0:
                dq.append((d, h, lo, c))
        if dq:
            self._panel[symbol] = dq

    def warmup_bars(self) -> int:
        # The panel is seeded out-of-band; intraday history is only used for
        # the current day's OHLC row, so no long warmup is required.
        return 8

    def seed_days_needed(self) -> int:
        """CALENDAR days of daily candles the startup seeder must fetch. The
        panel needs _depth TRADING rows (>= lookback_bars + skip_bars + 1 for a
        rebalance); ~1.6 calendar days cover one trading day (weekends +
        holidays), plus a buffer. WITHOUT this method the seeder falls back to
        its 400-day default -> only ~270 trading rows -> just under the 274-row
        rebalance floor -> factor silently stays breadth-gated (a no-op) live
        even though it works in unit tests seeded with contiguous rows."""
        return int(self._depth * 1.6) + 15

    def on_seeded(self) -> None:
        # Warmup replays decide() before _seed_daily_panels runs, so the first
        # rebalance already fired on an empty panel and left _days_since near 0.
        # Force the next live bar to rebalance now that real daily history
        # exists. A restart that restores the sleeve's saved state afterwards
        # (restore_state) puts the real rebalance clock back; without that,
        # every 08:50 recycle forced a rebalance and the monthly cadence ran
        # daily.
        self._days_since = 10 ** 9

    # ------------------------------------------------------------- signals
    def generate_signals(self, state: MarketState) -> list[Signal]:
        cfg = self.cfg
        d = state.ts.date().isoformat()
        if self._cur_date is None:
            self._cur_date = d
        elif d != self._cur_date:
            # sessions a restarted process missed are in the reseeded panel
            self._days_since += 1 + sessions_between(self._panel, self._cur_date, d)
            self._roll_day()
            self._cur_date = d

        for sym in state.bars:
            inst = state.instruments.get(sym)
            if inst is None or inst.kind != InstrumentKind.EQUITY:
                continue
            px = state.price(sym)
            if not (math.isfinite(px) and px > 0):
                continue
            t = self._today.get(sym)
            if t is None or t[0] != d:
                self._today[sym] = [d, px, px, px]
            else:
                t[1] = max(t[1], px)
                t[2] = min(t[2], px)
                t[3] = px

        # The clock restarts only when a basket forms. A rebalance refused for
        # breadth (panel still unseeded, say) used to restart it too, leaving
        # the sleeve flat for a full cycle after seeding.
        if self._days_since >= cfg.rebalance_bars and self._rebalance(_equities(state)):
            self._days_since = 0

        out: list[Signal] = []
        for sym, direction in sorted(self._dir.items()):
            stop = self._stops.get(sym)
            if stop is None or sym not in state.bars:
                continue
            out.append(Signal(
                strategy=self.name, symbol=sym, direction=direction,
                stop_distance=stop, expected_edge_R=cfg.expected_edge_R, tag=sym,
            ))
        return out

    def _roll_day(self) -> None:
        for sym, (d, h, lo, c) in self._today.items():
            dq = self._panel.setdefault(sym, deque(maxlen=self._depth))
            if dq and dq[-1][0] >= d:
                continue
            dq.append((d, h, lo, c))
        self._today.clear()

    # ------------------------------------------------------------- rebalance
    def _rebalance(self, tradeable: set[str]) -> bool:
        """Rank the names this sleeve can trade: the equities in the tier's
        view. The seeded panel covers every configured equity, so ranking all
        of it picked names outside the view, whose signals were then dropped,
        leaving a truncated basket that was no longer dollar-neutral, and
        checked min_universe against names the book could not hold. Returns
        whether a basket formed."""
        cfg = self.cfg
        need = cfg.lookback_bars + cfg.skip_bars + 1
        # names that fell out of the tier view stop receiving live rows; a
        # panel whose newest row is stale would otherwise rank on frozen
        # prices forever — drop it from the cross-section instead
        stale_before = None
        if self._cur_date is not None:
            stale_before = (date.fromisoformat(self._cur_date)
                            - timedelta(days=10)).isoformat()
        mom: dict[str, float] = {}
        lvol: dict[str, float] = {}
        atr_d: dict[str, float] = {}
        for sym, dq in sorted(self._panel.items()):
            if sym not in tradeable:
                continue
            if len(dq) < max(need, cfg.vol_lookback + 1, cfg.atr_n + 2):
                continue
            if stale_before is not None and dq[-1][0] < stale_before:
                continue
            arr = np.asarray([(h, lo, c) for _, h, lo, c in dq], dtype=float)
            c = arr[:, 2]
            if not np.all(np.isfinite(c[-need:])) or (c[-need:] <= 0).any():
                continue
            m = c[-1 - cfg.skip_bars] / c[-1 - cfg.lookback_bars] - 1.0
            r = np.diff(np.log(c[-(cfg.vol_lookback + 1):]))
            v = float(np.std(r)) if r.size > 2 else float("nan")
            a = _atr_daily(arr, cfg.atr_n)
            if (math.isfinite(m) and math.isfinite(v) and v > 0
                    and math.isfinite(a) and a > 0):
                mom[sym], lvol[sym], atr_d[sym] = m, -v, a   # -v: low vol attractive

        syms = sorted(mom)
        if len(syms) < cfg.min_universe:        # insufficient breadth -> full no-op
            self._dir, self._stops = {}, {}
            return False
        score = _zsum(mom, syms, _zsum(lvol, syms, None))
        ranked = sorted(syms, key=lambda s: score[s])
        k = min(cfg.top_k, len(ranked) // 2)
        new: dict[str, float] = {}
        # ranked[-k:] with k == 0 is the whole list, not an empty one
        for s in ranked[len(ranked) - k:]:
            new[s] = 1.0
        if cfg.market_neutral:
            for s in ranked[:k]:
                new[s] = -1.0
        self._dir = new
        self._stops = {s: cfg.atr_mult * atr_d[s] for s in new}
        return bool(new)

    # ----------------------------------------------------------- persistence
    def state_dict(self) -> dict:
        return {
            "panel": {s: [list(r) for r in dq] for s, dq in self._panel.items()},
            "today": {s: list(v) for s, v in self._today.items()},
            "dir": dict(self._dir),
            "stops": dict(self._stops),
            "days_since": self._days_since,
            "cur_date": self._cur_date,
        }

    def load_state(self, d: dict) -> None:
        # a key left out is kept as it is (see restore_state)
        if "panel" in d:
            self._panel = {s: deque([tuple(r) for r in rows], maxlen=self._depth)
                           for s, rows in d["panel"].items()}
        if "today" in d:
            self._today = {s: list(v) for s, v in d["today"].items()}
        self._dir = {k: float(v) for k, v in d.get("dir", {}).items()}
        self._stops = {k: float(v) for k, v in d.get("stops", {}).items()}
        self._days_since = d.get("days_since", 10 ** 9)
        self._cur_date = d.get("cur_date")

    def restore_state(self, d: dict) -> None:
        """Resume the held basket and the rebalance clock from a snapshot.
        Per symbol, the deeper of the panel held now and the saved one is
        kept, the current one on a tie: a panel seeded at this start is as
        deep and newer, while warm-up alone builds only a few rows, so the
        saved panel stands in where seeding failed."""
        self.load_state({k: v for k, v in d.items() if k not in ("panel", "today")})
        self._panel = merge_panels(self._panel, d.get("panel", {}), self._depth)


def merge_panels(current: dict, saved: dict, depth: int) -> dict:
    """Per symbol, the current rows unless the saved ones are deeper."""
    out = dict(current)
    for sym, rows in saved.items():
        if len(rows) > len(out.get(sym, ())):
            out[sym] = deque([tuple(r) for r in rows], maxlen=depth)
    return out


def _equities(state: MarketState) -> set[str]:
    return {s for s in state.bars
            if (inst := state.instruments.get(s)) is not None
            and inst.kind == InstrumentKind.EQUITY}


def _atr_daily(arr: np.ndarray, n: int) -> float:
    """Mean true range over the last n daily rows of (high, low, close)."""
    if arr.shape[0] < n + 1:
        return float("nan")
    h, lo, c = arr[-(n + 1):, 0], arr[-(n + 1):, 1], arr[-(n + 1):, 2]
    prev_c = c[:-1]
    tr = np.maximum(h[1:], prev_c) - np.minimum(lo[1:], prev_c)
    return float(np.mean(tr))


def _z(vals: dict[str, float], syms: list[str]) -> dict[str, float]:
    arr = np.array([vals[s] for s in syms], dtype=float)
    mu, sd = arr.mean(), arr.std()
    if sd == 0:
        return dict.fromkeys(syms, 0.0)
    return {s: float((vals[s] - mu) / sd) for s in syms}


def _zsum(vals: dict[str, float], syms: list[str], acc: dict[str, float] | None) -> dict[str, float]:
    z = _z(vals, syms)
    if acc is None:
        return z
    return {s: acc.get(s, 0.0) + z[s] for s in syms}
