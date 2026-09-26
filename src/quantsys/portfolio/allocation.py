"""Capital allocation across strategies (fractional Kelly) and the book-level
volatility targeter.

Kelly: f_s = clip(kelly_fraction * mu_s / var_s, 0, f_cap) * regime_weight_s,
with mu already shrunk toward zero by the edge estimator. Incubation: while a
strategy has too little history for the online stats to mean anything, it gets
a small floor allocation (it must trade to earn statistics — the classic
online-Kelly chicken-and-egg). The floor is withdrawn early if evidence is
already clearly negative.

Vol targeting runs on the PROPOSED BOOK against the instrument-return
covariance (EWMA + diagonal shrinkage). Decision note: the spec sketches
strategy-return covariance here; instrument covariance is used instead because
it is observable from day one (no own-track-record needed), well conditioned,
and directly measures the thing being capped — book P&L variance. Strategy
covariance still enters implicitly through per-strategy Kelly stats.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

import numpy as np

from quantsys.config.schema import KellyConfig, VolTargetConfig
from quantsys.core.types import AuditEvent, Instrument, RegimeState, bars_per_year
from quantsys.portfolio.book import TargetBook
from quantsys.strategies.base import OnlineEdgeStats


class KellyAllocator:
    def __init__(self, cfg: KellyConfig):
        self.cfg = cfg

    def allocate(
        self,
        stats: Mapping[str, OnlineEdgeStats],
        regime: RegimeState,
        active: list[str],
        audits: list[AuditEvent],
        tilts: Mapping[str, float] | None = None,
    ) -> dict[str, float]:
        cfg = self.cfg
        f: dict[str, float] = {}
        for s in sorted(active):
            st = stats[s]
            var = max(st.var, cfg.var_floor)
            f_raw = cfg.kelly_fraction * st.mean / var if var > 0 else 0.0
            f_s = float(np.clip(f_raw, 0.0, cfg.f_cap))
            if st.n_eff < cfg.ramp_obs and f_s < cfg.ramp_floor:
                # Incubation, unless evidence is already clearly negative. The
                # test is a t-stat on the shrunk mean: f_raw is mean / var in
                # Kelly units, so one tiny loss on a floored variance used to
                # read as overwhelming evidence and withdraw the floor.
                t = st.mean / math.sqrt(var / st.n_eff) if st.n_eff > 0 and var > 0 else 0.0
                f_s = 0.0 if t <= -cfg.ramp_withdraw_t else cfg.ramp_floor
                audits.append(AuditEvent("kelly", "incubation_floor",
                                         f"{s}: n_eff={st.n_eff:.0f} t={t:.2f} f->{f_s:.3f}"))
            if cfg.explore_floor > 0.0 and f_s < cfg.explore_floor:
                # forced, edge-agnostic exploration — paper-only plumbing
                # validation, NOT withdrawn by negative evidence. The default
                # (0.0) leaves live and the backtest gate fully honest.
                f_s = cfg.explore_floor
                audits.append(AuditEvent("kelly", "explore_floor",
                                         f"{s}: forced f->{f_s:.3f} (paper exploration)"))
            f_s *= regime.strategy_weights.get(s, 1.0)
            if tilts is not None:
                t = tilts.get(s, 1.0)
                if t != 1.0:
                    audits.append(AuditEvent("kelly", "regime_tilt",
                                             f"{s}: x{t:.3f} (learned regime edge)"))
                    f_s *= t
            f[s] = min(f_s, cfg.f_cap)

        gross = sum(f.values())
        if gross > cfg.gross_f_cap and gross > 0:
            scale = cfg.gross_f_cap / gross
            f = {k: v * scale for k, v in f.items()}
            audits.append(AuditEvent("kelly", "gross_f_cap",
                                     f"sum f {gross:.3f} -> {cfg.gross_f_cap:.3f}"))
        return f


class VolTargeter:
    def __init__(self, cfg: VolTargetConfig, bar_minutes: float):
        self.cfg = cfg
        self.bar_minutes = bar_minutes

    def scaler(
        self,
        book: TargetBook,
        prices: Mapping[str, float],
        instruments: Mapping[str, Instrument],
        cov_symbols: list[str],
        sigma_bar: np.ndarray | None,
        equity: float,
        audits: list[AuditEvent],
    ) -> float:
        """target_vol / annualised proposed-book vol, clipped to config bounds.

        A book symbol outside `cov_symbols` (fewer than cov_window + 1 bars)
        contributes nothing to the estimated variance, so the estimate is
        biased low: the scaler may then shrink the book but never grow it.
        """
        cfg = self.cfg
        if book.empty or equity <= 0:
            return 1.0
        if sigma_bar is None or not cov_symbols:
            audits.append(AuditEvent("vol_target", "missing_cov",
                                     f"no covariance history for {book.symbols()}: scaler 1.0"))
            return 1.0
        covered = set(cov_symbols)
        missing = [s for s in book.symbols() if s not in covered]
        nn = book.net_notional(prices, instruments)
        w = np.array([nn.get(s, 0.0) / equity for s in cov_symbols])
        var_bar = float(w @ sigma_bar @ w)
        if not math.isfinite(var_bar) or var_bar <= 1e-18:
            audits.append(AuditEvent("vol_target", "degenerate_var",
                                     f"book variance per bar {var_bar!r}: scaler 1.0"))
            return 1.0
        ann_vol = math.sqrt(var_bar * bars_per_year(self.bar_minutes))
        s = float(np.clip(cfg.annual_vol_target / ann_vol, cfg.scaler_min, cfg.scaler_max))
        if missing:
            detail = f"no covariance history for {missing}: scaler {s:.3f}"
            if s > 1.0:
                detail += " capped at 1.0"
                s = 1.0
            audits.append(AuditEvent("vol_target", "missing_cov", detail))
        audits.append(AuditEvent("vol_target", "scaler",
                                 f"book_vol={ann_vol:.4f} target={cfg.annual_vol_target} scaler={s:.3f}"))
        return s
