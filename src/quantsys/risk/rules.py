"""Exposure rules: each can only SHRINK the proposed book (never add risk),
each emits an audit event, and they run in a canonical order. Every operation
is monotone-decreasing and per-symbol scalings act group-jointly, but a group
scaled for one symbol also moves the net of any symbol its other legs share
with an offsetting group, so one pass can leave a netted symbol above a cap.
SizingEngine.finalize therefore re-verifies every cap on the lot-rounded
targets with cap_breaches().

Order: per-instrument -> sector -> correlation clusters -> ADV -> gross ->
net -> margin.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import numpy as np

from quantsys.config.schema import ExposureConfig
from quantsys.core.types import AuditEvent, Instrument
from quantsys.portfolio.book import TargetBook
from quantsys.portfolio.tiers import TierState

# relative slack when re-verifying caps on integer holdings: float noise only
_TOL = 1e-9


@dataclass
class RiskContext:
    equity: float
    prices: Mapping[str, float]
    instruments: Mapping[str, Instrument]
    cfg: ExposureConfig
    tier: TierState
    corr_symbols: list[str]
    corr: np.ndarray | None     # correlation matrix aligned to corr_symbols


def apply_exposure_rules(book: TargetBook, ctx: RiskContext, audits: list[AuditEvent]) -> set[str]:
    """Shrink `book` to the caps. Returns every symbol whose quantity a rule
    reduced, hedge legs of a scaled group included."""
    cut: set[str] = set()
    if book.empty:
        return cut
    _per_instrument_cap(book, ctx, audits, cut)
    _sector_cap(book, ctx, audits, cut)
    _corr_cluster_cap(book, ctx, audits, cut)
    _adv_cap(book, ctx, audits, cut)
    _global_cap(book, ctx, audits, cut, "gross", ctx.tier.gross_leverage_cap * ctx.equity)
    _net_cap(book, ctx, audits, cut)
    _margin_cap(book, ctx, audits, cut)
    return cut


def _scale_symbol(book: TargetBook, ctx: RiskContext, audits: list[AuditEvent], cut: set[str],
                  rule: str, sym: str, cur: float, cap: float) -> None:
    _scale_groups(book, book.groups_touching(sym), cap / cur, cut)
    audits.append(AuditEvent("risk", rule, f"{sym}: {cur:,.0f} -> {cap:,.0f}",
                             symbol=sym, before=cur, after=cap))


def _per_instrument_cap(book: TargetBook, ctx: RiskContext, audits: list[AuditEvent],
                        cut: set[str]) -> None:
    cap = ctx.cfg.per_instrument_frac * ctx.equity
    for sym, nn in sorted(book.net_notional(ctx.prices, ctx.instruments).items()):
        if abs(nn) > cap > 0:
            _scale_symbol(book, ctx, audits, cut, "per_instrument_cap", sym, abs(nn), cap)


def _sector_cap(book: TargetBook, ctx: RiskContext, audits: list[AuditEvent],
                cut: set[str]) -> None:
    cap = ctx.cfg.sector_frac * ctx.equity
    nn = book.net_notional(ctx.prices, ctx.instruments)
    sectors = _sectors(nn, ctx)
    for sec in sorted(sectors):
        gross = sum(abs(nn[s]) for s in sectors[sec])
        if gross > cap > 0:
            gids = {g for s in sectors[sec] for g in book.groups_touching(s)}
            _scale_groups(book, gids, cap / gross, cut)
            audits.append(AuditEvent("risk", "sector_cap", f"{sec}: {gross:,.0f} -> {cap:,.0f}",
                                     before=gross, after=cap))


def _corr_cluster_cap(book: TargetBook, ctx: RiskContext, audits: list[AuditEvent],
                      cut: set[str]) -> None:
    cap = ctx.cfg.corr_cluster_frac * ctx.equity
    nn = book.net_notional(ctx.prices, ctx.instruments)
    for cluster in _clusters(nn, ctx):
        gross = sum(abs(nn[c]) for c in cluster)
        if gross > cap > 0:
            gids = {g for s in cluster for g in book.groups_touching(s)}
            _scale_groups(book, gids, cap / gross, cut)
            audits.append(AuditEvent("risk", "corr_cluster_cap",
                                     f"{cluster}: {gross:,.0f} -> {cap:,.0f}",
                                     before=gross, after=cap))


def _adv_cap(book: TargetBook, ctx: RiskContext, audits: list[AuditEvent], cut: set[str]) -> None:
    for sym, q in sorted(book.net_qty().items()):
        inst = ctx.instruments.get(sym)
        if inst is None or not inst.adv or inst.adv <= 0:
            continue
        cap_qty = ctx.tier.adv_cap_pct * inst.adv
        if abs(q) > cap_qty > 0:
            _scale_groups(book, book.groups_touching(sym), cap_qty / abs(q), cut)
            audits.append(AuditEvent("risk", "adv_cap", f"{sym}: qty {q:,.0f} -> {cap_qty:,.0f}",
                                     symbol=sym, before=abs(q), after=cap_qty))


def _global_cap(book: TargetBook, ctx: RiskContext, audits: list[AuditEvent], cut: set[str],
                name: str, cap: float) -> None:
    gross = book.gross(ctx.prices, ctx.instruments)
    if gross > cap > 0:
        _scale_all(book, cap / gross, cut)
        audits.append(AuditEvent("risk", f"{name}_cap", f"{gross:,.0f} -> {cap:,.0f}",
                                 before=gross, after=cap))


def _net_cap(book: TargetBook, ctx: RiskContext, audits: list[AuditEvent], cut: set[str]) -> None:
    cap = ctx.cfg.net_frac * ctx.equity
    net = abs(book.net(ctx.prices, ctx.instruments))
    if net > cap > 0:
        _scale_all(book, cap / net, cut)
        audits.append(AuditEvent("risk", "net_cap", f"{net:,.0f} -> {cap:,.0f}",
                                 before=net, after=cap))


def _margin_cap(book: TargetBook, ctx: RiskContext, audits: list[AuditEvent], cut: set[str]) -> None:
    cap = ctx.cfg.margin_util_cap * ctx.equity
    nn = book.net_notional(ctx.prices, ctx.instruments)
    margin = sum(abs(v) * ctx.instruments[s].margin_rate for s, v in nn.items() if s in ctx.instruments)
    if margin > cap > 0:
        _scale_all(book, cap / margin, cut)
        audits.append(AuditEvent("risk", "margin_cap", f"{margin:,.0f} -> {cap:,.0f}",
                                 before=margin, after=cap))


def _scale_groups(book: TargetBook, gids: Iterable[str], factor: float, cut: set[str]) -> None:
    """Scale each group once, however many of its legs sit on the breach."""
    gids = set(gids)
    for gid in sorted(gids):
        book.scale_group(gid, factor)
    cut.update(c.symbol for c in book.components if c.group_id in gids)


def _scale_all(book: TargetBook, factor: float, cut: set[str]) -> None:
    book.scale_all(factor)
    cut.update(book.symbols())


def _sectors(symbols: Iterable[str], ctx: RiskContext) -> dict[str, list[str]]:
    sectors: dict[str, list[str]] = {}
    for sym in symbols:
        sec = (ctx.instruments[sym].sector or "UNKNOWN") if sym in ctx.instruments else "UNKNOWN"
        sectors.setdefault(sec, []).append(sym)
    return sectors


def _clusters(symbols: Iterable[str], ctx: RiskContext) -> list[list[str]]:
    """Connected components over |rho| >= threshold among `symbols` that have
    a correlation estimate; only components of two or more, each sorted."""
    if ctx.corr is None or len(ctx.corr_symbols) < 2:
        return []
    present = set(symbols)
    idx = {s: i for i, s in enumerate(ctx.corr_symbols)}
    in_book = [s for s in ctx.corr_symbols if s in present]
    out: list[list[str]] = []
    seen: set[str] = set()
    for s in in_book:
        if s in seen:
            continue
        cluster, stack = [], [s]
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            cluster.append(u)
            for v in in_book:
                if v not in seen and abs(ctx.corr[idx[u], idx[v]]) >= ctx.cfg.corr_threshold:
                    stack.append(v)
        if len(cluster) >= 2:
            out.append(sorted(cluster))
    return out


# ------------------------------------------------ re-verification on holdings
@dataclass(frozen=True)
class CapBreach:
    """One configured cap that a netted holding exceeds."""

    rule: str                  # the exposure rule's audit name, e.g. "sector_cap"
    key: str                   # symbol, sector, cluster members, or "book"
    symbols: tuple[str, ...]   # holdings the cap measures; () means the whole book
    current: float
    cap: float


def cap_breaches(net_qty: Mapping[str, float], ctx: RiskContext) -> list[CapBreach]:
    """Every configured cap the netted holding `net_qty` (symbol -> signed
    units) exceeds, in the canonical rule order. The same caps and grouping as
    the rules above, evaluated on a holding instead of scaling a book."""
    out: list[CapBreach] = []
    cfg, E = ctx.cfg, ctx.equity
    nn = _notional(net_qty, ctx)

    def over(cur: float, cap: float) -> bool:
        return cap > 0 and cur > cap * (1.0 + _TOL)

    cap = cfg.per_instrument_frac * E
    for sym in sorted(nn):
        if over(abs(nn[sym]), cap):
            out.append(CapBreach("per_instrument_cap", sym, (sym,), abs(nn[sym]), cap))
    cap = cfg.sector_frac * E
    for sec, members in sorted(_sectors(nn, ctx).items()):
        gross = sum(abs(nn[s]) for s in members)
        if over(gross, cap):
            out.append(CapBreach("sector_cap", sec, tuple(sorted(members)), gross, cap))
    cap = cfg.corr_cluster_frac * E
    for cluster in _clusters(nn, ctx):
        gross = sum(abs(nn[s]) for s in cluster)
        if over(gross, cap):
            out.append(CapBreach("corr_cluster_cap", ",".join(cluster), tuple(cluster), gross, cap))
    for sym in sorted(net_qty):
        inst = ctx.instruments.get(sym)
        if inst is None or not inst.adv or inst.adv <= 0:
            continue
        cap_qty = ctx.tier.adv_cap_pct * inst.adv
        if over(abs(net_qty[sym]), cap_qty):
            out.append(CapBreach("adv_cap", sym, (sym,), abs(net_qty[sym]), cap_qty))
    gross = sum(abs(v) for v in nn.values())
    if over(gross, ctx.tier.gross_leverage_cap * E):
        out.append(CapBreach("gross_cap", "book", (), gross, ctx.tier.gross_leverage_cap * E))
    net = abs(sum(nn.values()))
    if over(net, cfg.net_frac * E):
        out.append(CapBreach("net_cap", "book", (), net, cfg.net_frac * E))
    margin = sum(abs(v) * ctx.instruments[s].margin_rate for s, v in nn.items()
                 if s in ctx.instruments)
    if over(margin, cfg.margin_util_cap * E):
        out.append(CapBreach("margin_cap", "book", (), margin, cfg.margin_util_cap * E))
    return out


def _notional(net_qty: Mapping[str, float], ctx: RiskContext) -> dict[str, float]:
    out: dict[str, float] = {}
    for sym, q in net_qty.items():
        pv = ctx.instruments[sym].point_value if sym in ctx.instruments else 1.0
        out[sym] = q * ctx.prices.get(sym, 0.0) * pv
    return out
