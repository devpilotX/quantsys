"""Risk engine: account-level kill switches, the continuous drawdown
throttle, per-position hard stops (with trailing for trend-tagged positions),
stop cooldowns, and broker reconciliation.

Semantics of the two halt classes (deliberately different):
- KILL (daily loss / max drawdown): our state is trusted, the risk is the
  market -> FLATTEN everything, halt new risk.
- HALT (reconciliation mismatch): our state is NOT trusted -> freeze (no
  orders at all; flattening on top of a wrong book could double the error)
  and demand human attention. Broker is ground truth.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime

from quantsys.config.schema import DrawdownConfig
from quantsys.core.market_state import MarketState
from quantsys.core.types import Position, TargetPosition, session_date


@dataclass(frozen=True)
class RiskPre:
    risk_frac_eff: float
    drawdown: float
    throttle: float
    day_pnl: float
    kill_reason: str | None
    halted_reason: str | None


class StopTracker:
    """Hard catastrophic stops per (strategy, symbol); trailing optional.

    Strategy-level exits (z-score, hysteresis, time stops) are the primary
    exit path; this tracker is the independent backstop that fires even if a
    strategy misbehaves.
    """

    def __init__(self, trailing_strategies: set[str]):
        self.trailing = trailing_strategies
        self.entries: dict[str, dict] = {}  # "strategy|symbol" -> entry data

    @staticmethod
    def _key(strategy: str, symbol: str) -> str:
        return f"{strategy}|{symbol}"

    def refresh(self, targets: list[TargetPosition]) -> None:
        wanted: set[str] = set()
        for t in targets:
            if t.qty == 0 or t.stop_distance <= 0:
                continue
            k = self._key(t.strategy, t.symbol)
            wanted.add(k)
            e = self.entries.get(k)
            if e is None or (e["direction"] > 0) != (t.qty > 0):
                self.entries[k] = {
                    "symbol": t.symbol,
                    "strategy": t.strategy,
                    "direction": 1 if t.qty > 0 else -1,
                    "entry_price": t.ref_price,
                    "stop_distance": t.stop_distance,
                    "best_price": t.ref_price,
                }
        for k in list(self.entries):
            if k not in wanted:
                del self.entries[k]

    def update_prices(self, state: MarketState) -> None:
        for e in self.entries.values():
            if e["strategy"] not in self.trailing:
                continue
            px = state.price(e["symbol"])
            if not math.isfinite(px):
                continue
            e["best_price"] = max(e["best_price"], px) if e["direction"] > 0 else min(e["best_price"], px)

    def breached(self, state: MarketState) -> dict[tuple[str, str], str]:
        out: dict[tuple[str, str], str] = {}
        for e in self.entries.values():
            px = state.price(e["symbol"])
            if not math.isfinite(px):
                continue
            anchor = e["best_price"] if e["strategy"] in self.trailing else e["entry_price"]
            if e["direction"] > 0 and px <= anchor - e["stop_distance"]:
                out[(e["strategy"], e["symbol"])] = f"stop: {px:.2f} <= {anchor - e['stop_distance']:.2f}"
            elif e["direction"] < 0 and px >= anchor + e["stop_distance"]:
                out[(e["strategy"], e["symbol"])] = f"stop: {px:.2f} >= {anchor + e['stop_distance']:.2f}"
        return out

    def retain(self, symbols: Collection[str]) -> None:
        """Keeps only the entries (trailing state included) on `symbols`."""
        self.entries = {k: e for k, e in self.entries.items() if e["symbol"] in symbols}

    def to_dict(self) -> dict:
        return {"entries": self.entries}

    def load(self, d: dict) -> None:
        self.entries = {k: dict(v) for k, v in d.get("entries", {}).items()}


class RiskEngine:
    def __init__(self, cfg: DrawdownConfig, base_risk_frac: float,
                 trailing_strategies: set[str], stop_cooldown_bars: int = 12):
        self.cfg = cfg
        self.base_risk_frac = base_risk_frac
        self.stop_cooldown_bars = stop_cooldown_bars
        self.stops = StopTracker(trailing_strategies)
        self.hwm: float | None = None
        self.day_anchor: float | None = None
        self.day_date: str | None = None
        self.day_killed = False
        self.dd_killed = False
        self.halted_reason: str | None = None
        self.cooldowns: dict[str, int] = {}  # "strategy|symbol" -> blocked bars left

    # ----------------------------------------------------------------- core
    def pre_decide(self, ts: datetime, equity: float | None) -> RiskPre:
        # Each bar consumes one blocked bar; an entry lapses on the bar after
        # it reaches zero, so a cooldown of N blocks exactly the N decision
        # bars after the hit bar.
        for k in list(self.cooldowns):
            self.cooldowns[k] -= 1
            if self.cooldowns[k] < 0:
                del self.cooldowns[k]

        if equity is None or not math.isfinite(equity):
            # Unreadable equity is not evidence of anything. It must not become
            # a high-water mark or a day anchor (NaN there disables both kills
            # for good), and no new risk may be taken on it: halt this bar only.
            reason = f"equity not finite ({equity!r}): bar halted"
            if self.halted_reason is not None:
                reason = f"{self.halted_reason}; {reason}"
            return RiskPre(
                risk_frac_eff=0.0,
                drawdown=math.nan,
                throttle=0.0,
                day_pnl=math.nan,
                kill_reason=self._kill_reason(),
                halted_reason=reason,
            )

        d = str(session_date(ts))
        if d != self.day_date:
            self.day_date = d
            self.day_anchor = equity
            if self.cfg.auto_rearm_daily:
                self.day_killed = False
        if self.hwm is None or equity > self.hwm:
            self.hwm = equity
        if self.day_anchor is None:
            self.day_anchor = equity

        dd = max(0.0, 1.0 - equity / self.hwm) if self.hwm > 0 else 0.0
        day_pnl = equity - self.day_anchor
        if equity <= 0:
            self.dd_killed = True
        if day_pnl <= -self.cfg.daily_loss_limit * self.day_anchor:
            self.day_killed = True
        if dd >= self.cfg.kill_drawdown:
            self.dd_killed = True

        throttle = float(min(max(1.0 - dd / self.cfg.max_drawdown, 0.0), 1.0))
        return RiskPre(
            risk_frac_eff=self.base_risk_frac * throttle,
            drawdown=dd,
            throttle=throttle,
            day_pnl=day_pnl,
            kill_reason=self._kill_reason(),
            halted_reason=self.halted_reason,
        )

    def _kill_reason(self) -> str | None:
        return ("max_drawdown" if self.dd_killed else
                "daily_loss_limit" if self.day_killed else None)

    def register_stop_hits(self, hits: dict[tuple[str, str], str]) -> None:
        for (strategy, symbol) in hits:
            self.cooldowns[f"{strategy}|{symbol}"] = self.stop_cooldown_bars

    def in_cooldown(self, strategy: str, symbol: str) -> bool:
        return f"{strategy}|{symbol}" in self.cooldowns

    def retain_symbols(self, held: Collection[str]) -> None:
        """Drops stop entries, trailing state and cooldowns on every symbol not
        in `held`."""
        self.stops.retain(held)
        self.cooldowns = {k: v for k, v in self.cooldowns.items() if k.partition("|")[2] in held}

    def reconcile(self, internal: dict[str, int], broker: dict[str, int]) -> list[str]:
        """Broker is ground truth. Any mismatch freezes the engine."""
        mismatches = []
        for sym in sorted(set(internal) | set(broker)):
            a, b = internal.get(sym, 0), broker.get(sym, 0)
            if a != b:
                mismatches.append(f"{sym}: internal={a} broker={b}")
        if mismatches:
            self.halted_reason = "reconciliation: " + "; ".join(mismatches)
        return mismatches

    def rearm(self, baseline: float | None = None) -> None:
        """Manual operator action: clears the drawdown kill, the day kill and
        the reconciliation halt, and re-bases the drawdown reference.

        Without the re-base the next bar measures drawdown from the old
        high-water mark and kills again at the same equity. `baseline` is the
        equity to measure drawdown from; None means the next observed equity.
        The day anchor is cleared so the day-loss limit counts from the next
        observed equity.
        """
        if baseline is not None and not (math.isfinite(baseline) and baseline > 0):
            raise ValueError(f"rearm baseline must be finite and positive, got {baseline!r}")
        self.dd_killed = False
        self.day_killed = False
        self.halted_reason = None
        self.hwm = baseline
        self.day_anchor = None

    def clear_halt(self) -> None:
        """Clears only the reconciliation halt. Kills, the high-water mark and
        the day anchor are left as they are."""
        self.halted_reason = None

    def rebase_equity(self, old: float, new: float) -> None:
        """Operator capital change (deposit, withdrawal, deployable-cap edit):
        scale the high-water mark and the day anchor by new / old so the
        change is booked as neither a gain nor a loss."""
        for name, v in (("old", old), ("new", new)):
            if not (math.isfinite(v) and v > 0):
                raise ValueError(f"rebase_equity {name} must be finite and positive, got {v!r}")
        k = new / old
        if self.hwm is not None:
            self.hwm *= k
        if self.day_anchor is not None:
            self.day_anchor *= k

    # ---------------------------------------------------------- persistence
    def state_dict(self) -> dict:
        return {
            "hwm": self.hwm,
            "day_anchor": self.day_anchor,
            "day_date": self.day_date,
            "day_killed": self.day_killed,
            "dd_killed": self.dd_killed,
            "halted_reason": self.halted_reason,
            "cooldowns": dict(self.cooldowns),
            "stops": self.stops.to_dict(),
        }

    def load_state(self, d: dict) -> None:
        # a non-finite reference persisted by an older build is discarded: the
        # next readable equity takes its place
        self.hwm = _finite_or_none(d.get("hwm"))
        self.day_anchor = _finite_or_none(d.get("day_anchor"))
        self.day_date = d.get("day_date")
        self.day_killed = d.get("day_killed", False)
        self.dd_killed = d.get("dd_killed", False)
        self.halted_reason = d.get("halted_reason")
        self.cooldowns = {k: int(v) for k, v in d.get("cooldowns", {}).items()}
        self.stops.load(d.get("stops", {}))


def positions_net(positions: dict[str, Position]) -> dict[str, int]:
    return {s: p.qty for s, p in positions.items() if p.qty != 0}


def _finite_or_none(v: float | None) -> float | None:
    return v if v is not None and math.isfinite(v) else None
