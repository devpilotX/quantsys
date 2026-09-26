"""Property tests for the sizing and exposure invariants (CONTRIBUTING rules 7
and 8): caps only shrink, the final integer book never breaches a configured
cap, min-lot promotion never breaches one either, a bad stop is never sized,
and no hedge leg is ever orphaned, in the targets or in the orders.

The cap checker here is written independently of risk/rules.py so the tests
do not grade the implementation with itself.
"""

from __future__ import annotations

import math

import numpy as np
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.conftest import make_hist, make_inst, make_state

from quantsys.config.schema import AppConfig, CostConfig, ExposureConfig, SizingConfig
from quantsys.core.market_state import MarketState
from quantsys.core.types import (
    ExecutionStyle,
    InstrumentKind,
    LegSpec,
    Position,
    Signal,
    TargetPosition,
)
from quantsys.costs import CostModel
from quantsys.engine.decision import DecisionEngine
from quantsys.engine.orders import diff_orders
from quantsys.portfolio.book import Component, TargetBook
from quantsys.portfolio.sizing import SizingEngine
from quantsys.portfolio.tiers import TierLadder, TierState
from quantsys.risk.rules import RiskContext, apply_exposure_rules
from quantsys.strategies.base import OnlineEdgeStats, Strategy

_TOL = 1e-9
_PROPS = settings(max_examples=150, deadline=None, database=None)

_frac = st.floats(-1.0, 1.0).filter(lambda x: abs(x) > 1e-3)


@st.composite
def _market(draw):
    n = draw(st.integers(2, 5))
    syms = [f"S{i}" for i in range(n)]
    insts, prices = {}, {}
    for s in syms:
        lot = draw(st.sampled_from([1, 1, 25, 75, 500]))
        insts[s] = make_inst(
            s, kind=InstrumentKind.EQUITY if lot == 1 else InstrumentKind.FUTURE,
            lot_size=lot, sector=draw(st.sampled_from(["a", "b", "c"])),
            adv=draw(st.one_of(st.none(), st.floats(1e3, 1e7))),
            margin_rate=draw(st.floats(0.1, 1.0)))
        prices[s] = draw(st.floats(10.0, 5000.0))
    equity = draw(st.floats(1e5, 1e8))
    cluster = {s: draw(st.integers(0, 2)) for s in syms}
    with_cov = [s for s in syms if draw(st.booleans())]
    corr = None
    if len(with_cov) >= 2:
        corr = np.array([[1.0 if a == b else (0.9 if cluster[a] == cluster[b] else 0.2)
                          for b in with_cov] for a in with_cov])
    caps = ExposureConfig(
        per_instrument_frac=draw(st.floats(0.05, 0.6)), sector_frac=draw(st.floats(0.1, 1.2)),
        net_frac=draw(st.floats(0.1, 2.0)), margin_util_cap=draw(st.floats(0.1, 1.5)),
        corr_threshold=0.7, corr_cluster_frac=draw(st.floats(0.1, 1.2)))
    tier = TierState("T", 0, 40, 7, ExecutionStyle.LIMIT_SMART,
                     draw(st.floats(0.001, 0.1)), 0.0, 0.2, draw(st.floats(0.3, 3.0)))
    ctx = RiskContext(equity=equity, prices=prices, instruments=insts, cfg=caps, tier=tier,
                      corr_symbols=with_cov if corr is not None else [], corr=corr)
    state = make_state({s: make_hist(np.full(60, prices[s])) for s in syms}, insts,
                       equity=equity)
    return syms, ctx, state


@st.composite
def _book(draw, syms, ctx):
    """1..6 groups, single-leg or two-leg, on overlapping symbols. Each leg is
    a signed fraction of equity."""
    book = TargetBook()
    for i in range(draw(st.integers(1, 6))):
        if draw(st.booleans()):
            a, b = draw(st.lists(st.sampled_from(syms), min_size=2, max_size=2, unique=True))
            fa = draw(_frac)
            ratio = draw(st.floats(-2.0, 2.0).filter(lambda x: abs(x) > 1e-2))
            legs = [(a, fa), (b, fa * ratio)]
            sig = Signal("s", a, math.copysign(1.0, fa), 0.01 * ctx.prices[a],
                         legs=(LegSpec(a, 1.0), LegSpec(b, ratio)), tag=f"g{i}")
        else:
            a = draw(st.sampled_from(syms))
            fa = draw(_frac)
            legs = [(a, fa)]
            sig = Signal("s", a, math.copysign(1.0, fa), 0.01 * ctx.prices[a], tag=f"g{i}")
        comps = [Component(sym, f * ctx.equity / ctx.prices[sym], "s", sig.group_id,
                           0.01 * ctx.prices[sym], ctx.prices[sym], is_parent=(j == 0))
                 for j, (sym, f) in enumerate(legs)]
        book.add_group(comps, sig)
    return book


def _breaches(targets: list[TargetPosition], ctx: RiskContext) -> list[str]:
    q: dict[str, float] = {}
    for t in targets:
        q[t.symbol] = q.get(t.symbol, 0.0) + t.qty
    E, cfg, ins = ctx.equity, ctx.cfg, ctx.instruments
    nn = {s: v * ctx.prices[s] * ins[s].point_value for s, v in q.items()}
    out: list[str] = []

    def over(cur: float, cap: float, what: str) -> None:
        if cap > 0 and cur > cap * (1 + _TOL):
            out.append(f"{what}: {cur:,.2f} > {cap:,.2f}")

    for s, v in nn.items():
        over(abs(v), cfg.per_instrument_frac * E, f"instrument {s}")
        if ins[s].adv:
            over(abs(q[s]), ctx.tier.adv_cap_pct * ins[s].adv, f"adv {s}")
    sectors: dict[str, float] = {}
    for s, v in nn.items():
        key = ins[s].sector or "UNKNOWN"
        sectors[key] = sectors.get(key, 0.0) + abs(v)
    for sec, g in sectors.items():
        over(g, cfg.sector_frac * E, f"sector {sec}")
    if ctx.corr is not None:
        held = [s for s in ctx.corr_symbols if s in nn]
        idx = {s: i for i, s in enumerate(ctx.corr_symbols)}
        parent = {s: s for s in held}

        def root(s: str) -> str:
            while parent[s] != s:
                s = parent[s]
            return s

        for a in held:
            for b in held:
                if abs(ctx.corr[idx[a], idx[b]]) >= cfg.corr_threshold:
                    parent[root(a)] = root(b)
        clusters: dict[str, list[str]] = {}
        for s in held:
            clusters.setdefault(root(s), []).append(s)
        for members in clusters.values():
            if len(members) >= 2:
                over(sum(abs(nn[s]) for s in members), cfg.corr_cluster_frac * E,
                     f"cluster {sorted(members)}")
    over(sum(abs(v) for v in nn.values()), ctx.tier.gross_leverage_cap * E, "gross")
    over(abs(sum(nn.values())), cfg.net_frac * E, "net")
    over(sum(abs(v) * ins[s].margin_rate for s, v in nn.items()), cfg.margin_util_cap * E,
         "margin")
    return out


def _legs_by_group(book: TargetBook) -> dict[str, set[str]]:
    return {gid: {c.symbol for c in comps} for gid, comps in book.group_components().items()}


# ------------------------------------------------------------ exposure caps
@_PROPS
@given(data=st.data())
def test_exposure_rules_only_shrink_and_keep_hedge_ratios(data):
    syms, ctx, _ = data.draw(_market())
    book = data.draw(_book(syms, ctx))
    before = {(c.group_id, c.symbol): c.qty for c in book.components}
    apply_exposure_rules(book, ctx, [])
    for gid, comps in book.group_components().items():
        f = comps[0].qty / before[(gid, comps[0].symbol)]
        assert 0.0 <= f <= 1.0 + 1e-12
        for c in comps:
            assert math.isclose(c.qty / before[(gid, c.symbol)], f, rel_tol=1e-9)


@_PROPS
@given(data=st.data(), floor=st.sampled_from([0.0, 1_000.0, 5_000.0, 20_000.0]))
def test_final_targets_never_breach_a_cap_or_orphan_a_leg(data, floor):
    syms, ctx, state = data.draw(_market())
    book = data.draw(_book(syms, ctx))
    legs = _legs_by_group(book)
    sizer = SizingEngine(SizingConfig(), CostModel(CostConfig()), floor)
    apply_exposure_rules(book, ctx, [])
    capped = {(c.group_id, c.symbol): c.qty for c in book.components}
    targets = sizer.finalize(book, ctx.tier, state, {}, {}, [], ctx=ctx)

    assert _breaches(targets, ctx) == []
    got: dict[str, set[str]] = {}
    for t in targets:
        got.setdefault(t.group_id, set()).add(t.symbol)
        lot = ctx.instruments[t.symbol].lot_size
        assert t.qty != 0 and t.qty % lot == 0
        frac = capped[(t.group_id, t.symbol)]
        assert abs(t.qty) <= abs(frac) * (1 + 1e-12), "a cap grew a position"
        assert math.copysign(1.0, t.qty) == math.copysign(1.0, frac)
        assert abs(t.qty) * ctx.prices[t.symbol] >= floor
    for gid, symbols in got.items():
        assert symbols == legs[gid], f"{gid} lost a leg: {symbols} of {legs[gid]}"


@_PROPS
@given(data=st.data(), risk_scale=st.floats(0.0, 1.0))
def test_min_lot_promotion_never_breaches_a_cap(data, risk_scale):
    syms, ctx, state = data.draw(_market())
    book = data.draw(_book(syms, ctx))
    # add single-leg futures groups smaller than one lot: promotion candidates
    for i, s in enumerate(syms):
        inst = ctx.instruments[s]
        if inst.lot_size > 1 and data.draw(st.booleans()):
            q = data.draw(st.floats(0.05, 0.95)) * inst.lot_size * data.draw(st.sampled_from([1, -1]))
            sig = Signal("p", s, math.copysign(1.0, q), 0.01 * ctx.prices[s], tag=f"p{i}")
            book.add_group([Component(s, q, "p", sig.group_id, 0.01 * ctx.prices[s],
                                      ctx.prices[s])], sig)
    frac = {(c.group_id, c.symbol): c.qty for c in book.components}
    sizer = SizingEngine(SizingConfig(min_lot_promotion=True), CostModel(CostConfig()), 0.0)
    apply_exposure_rules(book, ctx, [])
    audits: list = []
    targets = sizer.finalize(book, ctx.tier, state, {}, {}, audits, ctx=ctx, risk_scale=risk_scale)

    assert _breaches(targets, ctx) == []
    promoted = {a.detail for a in audits if a.rule == "min_lot_promotion"}
    for t in targets:
        if t.group_id in promoted:
            inst = ctx.instruments[t.symbol]
            assert abs(t.qty) == inst.lot_size
            assert math.copysign(1.0, t.qty) == math.copysign(1.0, frac[(t.group_id, t.symbol)])
            lot_risk = inst.lot_size * t.stop_distance * inst.point_value
            assert lot_risk <= 0.005 * ctx.equity * risk_scale * (1 + 1e-12)


# ------------------------------------------------------------------ sizing
@_PROPS
@given(stop=st.one_of(st.floats(allow_nan=True, allow_infinity=True), st.just(0.0)),
       direction=st.sampled_from([1.0, -1.0, 0.5, -0.25]),
       price=st.floats(10.0, 5000.0), budget=st.floats(100.0, 1e6))
def test_group_risk_matches_its_budget_and_bad_stops_are_never_sized(stop, direction, price, budget):
    state = make_state({"X": make_hist(np.full(60, price))}, {"X": make_inst("X")}, equity=1e8)
    audits: list = []
    sizer = SizingEngine(SizingConfig(), CostModel(CostConfig()), 0.0)
    book = sizer.build_raw([Signal("s", "X", direction, stop)], {"s": 1.0}, budget, state, audits)
    if not (math.isfinite(stop) and stop > 0):
        assert book.empty
        assert [a.rule for a in audits] == ["bad_stop"]
        return
    (c,) = book.components
    stop_eff = max(stop, SizingConfig().atr_min_stop_ticks * 0.05)
    assert math.copysign(1.0, c.qty) == math.copysign(1.0, direction)
    assert math.isclose(abs(c.qty) * stop_eff, budget, rel_tol=1e-9)


# ------------------------------------------------------------------ orders
@_PROPS
@given(data=st.data())
def test_diff_orders_moves_hedge_legs_together(data):
    syms = ["A", "B", "C", "D"]
    insts = {s: make_inst(s) for s in syms}
    prices = {s: data.draw(st.floats(10.0, 2000.0)) for s in syms}
    targets: list[TargetPosition] = []
    pairs: list[tuple[str, str]] = []
    for i in range(data.draw(st.integers(1, 3))):
        a, b = data.draw(st.lists(st.sampled_from(syms), min_size=2, max_size=2, unique=True))
        qa = data.draw(st.integers(1, 5000)) * data.draw(st.sampled_from([1, -1]))
        qb = data.draw(st.integers(1, 5000)) * data.draw(st.sampled_from([1, -1]))
        targets += [TargetPosition(a, qa, "mr", f"mr:{i}", 1.0, prices[a]),
                    TargetPosition(b, qb, "mr", f"mr:{i}", 1.0, prices[b])]
        pairs.append((a, b))
    if data.draw(st.booleans()):
        s = data.draw(st.sampled_from(syms))
        targets.append(TargetPosition(s, data.draw(st.integers(-5000, 5000).filter(bool)),
                                      "trend", f"trend:{s}", 1.0, prices[s]))
    positions = {s: Position(s, data.draw(st.integers(-6000, 6000)), prices[s]) for s in syms
                 if data.draw(st.booleans())}
    tier = TierState("T", 0, 40, 7, ExecutionStyle.LIMIT_SMART, 0.02, 0.0,
                     data.draw(st.floats(0.0, 0.5)), 2.0)
    floor = data.draw(st.sampled_from([0.0, 5_000.0, 50_000.0]))
    orders = diff_orders(targets, positions, insts, prices, tier, floor)

    net: dict[str, int] = {}
    for t in targets:
        net[t.symbol] = net.get(t.symbol, 0) + t.qty
    sent = {o.symbol for o in orders}
    for a, b in pairs:
        moving = {s for s in (a, b)
                  if net.get(s, 0) != (positions[s].qty if s in positions else 0)}
        assert not (moving & sent) or moving <= sent, (
            f"pair {a}/{b}: legs {sorted(moving)} need orders, only {sorted(sent & moving)} sent")



# ------------------------------------------------------------------ engine
class _Fixed(Strategy):
    def __init__(self, name: str, signals: list[Signal]) -> None:
        super().__init__(name)
        self.signals = signals

    def generate_signals(self, state: MarketState) -> list[Signal]:
        return list(self.signals)

    def warmup_bars(self) -> int:
        return 0


_UNI = [
    {"symbol": "EA", "sector": "a", "adv": 4e6, "margin_rate": 1.0},
    {"symbol": "EB", "sector": "a", "adv": 2e5, "margin_rate": 1.0},
    {"symbol": "FC", "kind": "FUTURE", "lot_size": 50, "sector": "b", "adv": 3e5,
     "margin_rate": 0.15},
    {"symbol": "FD", "kind": "FUTURE", "lot_size": 500, "sector": "c", "adv": 5e6,
     "margin_rate": 0.2},
]
_PX = {"EA": 250.0, "EB": 1800.0, "FC": 24_000.0, "FD": 90.0}
_LOT = {u["symbol"]: u.get("lot_size", 1) for u in _UNI}


@_PROPS
@given(data=st.data(), equity=st.sampled_from([1e6, 1e7, 5e7]))
def test_engine_book_respects_every_cap_and_moves_hedges_whole(data, equity):
    syms = list(_PX)
    signals = []
    for i in range(data.draw(st.integers(1, 5))):
        a = data.draw(st.sampled_from(syms))
        stop = _PX[a] * data.draw(st.floats(0.0005, 0.05))
        direction = data.draw(st.sampled_from([1.0, -1.0, 0.5, -0.5]))
        if data.draw(st.booleans()):
            b = data.draw(st.sampled_from([s for s in syms if s != a]))
            ratio = data.draw(st.floats(-1.5, 1.5).filter(lambda x: abs(x) > 0.05))
            signals.append(Signal("stub", a, direction, stop,
                                  legs=(LegSpec(a, 1.0), LegSpec(b, ratio)), tag=f"p{i}"))
        else:
            signals.append(Signal("stub", a, direction, stop, tag=f"s{i}"))
    positions = {s: Position(s, _LOT[s] * data.draw(st.integers(-400, 400)), _PX[s])
                 for s in syms if data.draw(st.booleans())}
    cfg = AppConfig.model_validate({
        "engine": {"decision_bar_minutes": 5, "min_order_notional": 5000.0},
        "sizing": {"enforce_cost_gate": False, "min_lot_promotion": True},
        "kelly": {"f_cap": 0.30, "ramp_floor": 0.30, "explore_floor": 0.30},
        "universe": _UNI,
    })
    eng = DecisionEngine(cfg)
    eng.strategies = [_Fixed("stub", signals)]
    k = cfg.kelly
    eng.edge_stats = {"stub": OnlineEdgeStats(k.edge_halflife_bars, k.prior_obs, k.var_floor)}
    eng.regime_stats = {"stub": {}}
    state = make_state({s: make_hist(np.full(60, px)) for s, px in _PX.items()},
                       eng.instruments, equity=equity, positions=positions)
    d = eng.decide(state)

    tier = TierLadder(cfg.tiers).resolve(equity)
    ctx = RiskContext(equity, _PX, eng.instruments, cfg.exposure, tier, [], None)
    assert _breaches(list(d.targets), ctx) == []
    legs = {s.group_id: {leg.symbol for leg in s.resolved_legs()} for s in signals}
    got: dict[str, set[str]] = {}
    for t in d.targets:
        got.setdefault(t.group_id, set()).add(t.symbol)
    for gid, symbols in got.items():
        assert symbols == legs[gid], f"{gid} lost a leg"
    net: dict[str, int] = {}
    for t in d.targets:
        net[t.symbol] = net.get(t.symbol, 0) + t.qty
    sent = {o.symbol for o in d.orders}
    for gid, symbols in got.items():
        moving = {s for s in symbols
                  if net.get(s, 0) != (positions[s].qty if s in positions else 0)}
        assert not (moving & sent) or moving <= sent, f"{gid}: {sorted(moving - sent)} left behind"
