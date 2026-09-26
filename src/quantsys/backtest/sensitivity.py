"""Parameter-sensitivity sweep — perturb one tunable at a time around the
baseline, re-run, and report how OOS Sharpe/CAGR/maxDD move. A strategy whose
edge survives only at one knob setting is overfit; this exposes that and feeds
``n_trials`` into the deflated-Sharpe deflation (every variant tried counts).

Every variant counts, including one whose value the config refuses and one
whose result is identical to another's: dropping either lowers n_trials and
makes the deflation easier to pass. An error raised by the backtest itself is
a bug, not a failed trial, and propagates.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from datetime import datetime

from quantsys.backtest.loop import run_backtest
from quantsys.backtest.metrics import compute_metrics
from quantsys.config.schema import AppConfig
from quantsys.core.types import Bar

log = logging.getLogger("quantsys.backtest.sensitivity")

# What setting a variant's value can legitimately raise: a value its field
# cannot take or represent. Anything else, and anything the run raises, is a
# bug and propagates.
_VARIANT_ERRORS = (ValueError, ArithmeticError)


@dataclass
class SweepPoint:
    param: str
    value: float
    sharpe: float | None
    cagr: float | None
    max_dd: float | None
    n_trades: int


@dataclass
class SensitivityResult:
    points: list[SweepPoint] = field(default_factory=list)
    n_trials: int = 0
    failures: list[str] = field(default_factory=list)   # "param=value: error", counted in n_trials


def _set_path(cfg: AppConfig, path: str, value) -> None:
    obj = cfg
    parts = path.split(".")
    for p in parts[:-1]:
        obj = getattr(obj, p)
    cur = getattr(obj, parts[-1])
    setattr(obj, parts[-1], type(cur)(value))


def sweep(
    base_cfg: AppConfig,
    bars: list[tuple[datetime, dict[str, Bar]]],
    starting_equity: float,
    grid: dict[str, list[float]],
    warmup_bars: int = 0,
) -> SensitivityResult:
    res = SensitivityResult()
    seen: dict[tuple[float, ...], str] = {}
    for param, values in grid.items():
        for v in values:
            res.n_trials += 1
            label = f"{param}={v!r}"
            cfg = copy.deepcopy(base_cfg)
            try:
                _set_path(cfg, param, v)
            except _VARIANT_ERRORS as exc:
                res.failures.append(f"{label}: {exc!r}")
                log.warning("sweep variant %s could not be configured and still counts as "
                            "a trial: %r", label, exc)
                continue
            r = run_backtest(cfg, iter(bars), starting_equity, warmup_bars=warmup_bars)
            daily = r.daily_equity()
            key = tuple(e for _, e in daily)
            if key in seen:
                log.warning("sweep variant %s produced a result identical to %s: the knob did "
                            "not bind here, but it still counts as a trial", label, seen[key])
            else:
                seen[key] = label
            m = compute_metrics(daily, r.trades, r.total_fees,
                                r.traded_notional, starting_equity, start=r.scored_from)
            res.points.append(SweepPoint(
                param=param, value=float(v), sharpe=m.get("sharpe"),
                cagr=m.get("cagr"), max_dd=m.get("max_dd"),
                n_trades=m.get("n_trades", 0),
            ))
    if res.n_trials and len(res.failures) == res.n_trials:
        raise RuntimeError(f"every sweep variant failed ({res.n_trials}), so there is no "
                           f"result and no honest n_trials: {'; '.join(res.failures)}")
    return res
