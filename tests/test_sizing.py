import pytest
from tests.conftest import gbm, make_hist, make_inst, make_state

from quantsys.config.schema import CostConfig, ExposureConfig, SizingConfig
from quantsys.core.types import (
    ExecutionStyle,
    InstrumentKind,
    LegSpec,
    Position,
    Signal,
)
from quantsys.costs import CostModel
from quantsys.portfolio.book import Component, TargetBook
from quantsys.portfolio.sizing import SizingEngine
from quantsys.portfolio.tiers import TierState
from quantsys.risk.rules import RiskContext, apply_exposure_rules


def _tier(min_cost_multiple=0.0, adv_cap_pct=1.0, band=0.2, lev=2.0) -> TierState:
    return TierState("T", 0, 10, 4, ExecutionStyle.LIMIT_SMART,
                     adv_cap_pct, min_cost_multiple, band, lev)


def _sizer(min_notional=0.0) -> SizingEngine:
    return SizingEngine(SizingConfig(), CostModel(CostConfig()), min_notional)


def _state_with(symbols: dict[str, float], equity=1_000_000.0, **inst_kw):
    bars = {s: make_hist(gbm(60, start=p, vol=0.0001, seed=i))
            for i, (s, p) in enumerate(symbols.items())}
    insts = {s: make_inst(s, **inst_kw) for s in symbols}
    return make_state(bars, insts, equity=equity)


def test_qty_formula_exact():
    state = _state_with({"X": 100.0})
    sig = Signal("s", "X", direction=1.0, stop_distance=10.0)
    book = _sizer().build_raw([sig], {"s": 0.5}, 1_000_000 * 0.006, state, [])
    # qty = E*risk_frac*f / stop = 6000*0.5/10 = 300
    assert book.net_qty()["X"] == pytest.approx(300.0, rel=1e-3)
    short = Signal("s", "X", direction=-1.0, stop_distance=10.0)
    book2 = _sizer().build_raw([short], {"s": 0.5}, 6000.0, state, [])
    assert book2.net_qty()["X"] == pytest.approx(-300.0, rel=1e-3)


def test_conviction_split_within_strategy():
    state = _state_with({"X": 100.0, "Y": 100.0})
    sigs = [Signal("s", "X", 1.0, 10.0), Signal("s", "Y", 0.5, 10.0)]
    book = _sizer().build_raw(sigs, {"s": 1.0}, 9000.0, state, [])
    nq = book.net_qty()
    assert nq["X"] == pytest.approx(2 * nq["Y"], rel=1e-6)  # 2/3 vs 1/3 risk share
    assert nq["X"] + nq["Y"] == pytest.approx(900.0, rel=1e-6)


def test_pair_legs_hedge_ratio_exact():
    state = _state_with({"A": 100.0, "B": 50.0})
    pa, pb = state.price("A"), state.price("B")  # actual GBM closes near 100/50
    sig = Signal("mr", "A", direction=1.0, stop_distance=5.0,
                 legs=(LegSpec("A", 1.0), LegSpec("B", -2.0)), tag="A|B")
    book = _sizer().build_raw([sig], {"mr": 1.0}, 5000.0, state, [])
    nq = book.net_qty()
    qa, qb = nq["A"], nq["B"]
    assert qa == pytest.approx(1000.0, rel=1e-12)            # 5000/5, price-free
    assert qb == pytest.approx(-2.0 * qa * pa / pb, rel=1e-9)
    assert (qb * pb) / (qa * pa) == pytest.approx(-2.0, rel=1e-9)


def test_lot_rounding_floors_and_drops_zero():
    state = _state_with({"F": 100.0}, kind=InstrumentKind.FUTURE, lot_size=65)
    sig = Signal("s", "F", 1.0, stop_distance=10.0)
    book = _sizer().build_raw([sig], {"s": 1.0}, 3000.0, state, [])  # 300 raw
    targets = _sizer().finalize(book, _tier(), state, {}, {}, [])
    assert targets[0].qty == 260  # floor(300/65)=4 lots
    tiny = _sizer().build_raw([sig], {"s": 1.0}, 500.0, state, [])   # 50 raw < 1 lot
    audits = []
    assert _sizer().finalize(tiny, _tier(), state, {}, {}, audits) == []
    assert any(a.rule == "rounds_to_zero" for a in audits)


def test_pair_dropped_whole_if_one_leg_rounds_to_zero():
    state = _state_with({"A": 100.0, "B": 50.0}, kind=InstrumentKind.FUTURE, lot_size=500)
    sig = Signal("mr", "A", 1.0, 5.0, legs=(LegSpec("A", 1.0), LegSpec("B", -0.01)))
    book = _sizer().build_raw([sig], {"mr": 1.0}, 5000.0, state, [])
    targets = _sizer().finalize(book, _tier(), state, {}, {}, [])
    assert targets == []  # B leg rounds to zero -> no orphaned unhedged A leg


def test_cost_gate_blocks_thin_edges_at_small_tier():
    state = _state_with({"X": 100.0}, equity=100_000.0)
    sig = Signal("s", "X", 1.0, stop_distance=2.0, expected_edge_R=0.10)
    sizer = _sizer()
    book = sizer.build_raw([sig], {"s": 1.0}, 600.0, state, [])  # qty 300
    audits = []
    blocked = sizer.finalize(book, _tier(min_cost_multiple=5.0), state, {}, {}, audits)
    assert blocked == []
    assert any(a.rule == "cost_gate" for a in audits)
    book2 = sizer.build_raw([sig], {"s": 1.0}, 600.0, state, [])
    allowed = sizer.finalize(book2, _tier(min_cost_multiple=0.05), state, {}, {}, [])
    assert allowed and allowed[0].qty == 300


def test_cost_gate_can_be_disabled_for_paper_exploration():
    # paper exploration sets sizing.enforce_cost_gate=False so the order ->
    # fill -> reconcile -> P&L plumbing is exercised even on trades the honest
    # gate would veto. The default (True) is asserted by the test above.
    state = _state_with({"X": 100.0}, equity=100_000.0)
    sig = Signal("s", "X", 1.0, stop_distance=2.0, expected_edge_R=0.10)
    sizer = SizingEngine(SizingConfig(enforce_cost_gate=False), CostModel(CostConfig()), 0.0)
    book = sizer.build_raw([sig], {"s": 1.0}, 600.0, state, [])
    audits = []
    allowed = sizer.finalize(book, _tier(min_cost_multiple=5.0), state, {}, {}, audits)
    assert allowed and allowed[0].qty == 300       # blocked with gate on, allowed off
    assert not any(a.rule == "cost_gate" for a in audits)


def test_cost_gate_skips_held_positions():
    state = _state_with({"X": 100.0}, equity=100_000.0)
    sig = Signal("s", "X", 1.0, stop_distance=2.0, expected_edge_R=0.10)
    sizer = _sizer()
    book = sizer.build_raw([sig], {"s": 1.0}, 600.0, state, [])
    held = {"X": Position("X", 280, 99.0)}  # same direction -> not a new trade
    targets = sizer.finalize(book, _tier(min_cost_multiple=5.0), state, held, {}, [])
    assert targets and targets[0].qty == 300


def _caps(state) -> RiskContext:
    """Default exposure caps for `state`: what a promotion is verified against."""
    prices = {s: state.price(s) for s in state.bars}
    return RiskContext(state.equity, prices, state.instruments, ExposureConfig(), _tier(), [], None)


def test_min_lot_promotion_unlocks_single_future_lot():
    """Index-futures unlock: a FUTURE whose risk budget rounds below one lot
    takes exactly ONE lot when promotion is on and the lot's rupee risk fits
    promotion_max_risk_frac of equity. (Promotion off => stays untraded, see
    test_lot_rounding_floors_and_drops_zero.)"""
    state = _state_with({"F": 100.0}, equity=50_000_000.0,
                        kind=InstrumentKind.FUTURE, lot_size=65)
    sizer = SizingEngine(SizingConfig(min_lot_promotion=True),
                         CostModel(CostConfig()), 0.0)
    for direction, want in ((1.0, 65), (-1.0, -65)):
        sig = Signal("s", "F", direction, stop_distance=10.0)
        book = sizer.build_raw([sig], {"s": 1.0}, 500.0, state, [])  # 50 raw < 1 lot
        audits = []
        targets = sizer.finalize(book, _tier(), state, {}, {}, audits, ctx=_caps(state))
        assert [t.qty for t in targets] == [want]       # lot risk 650 <= 0.5% of 5e7
        assert any(a.rule == "min_lot_promotion" for a in audits)


def test_min_lot_promotion_needs_caps_to_verify_against():
    state = _state_with({"F": 100.0}, equity=50_000_000.0,
                        kind=InstrumentKind.FUTURE, lot_size=65)
    sizer = SizingEngine(SizingConfig(min_lot_promotion=True), CostModel(CostConfig()), 0.0)
    book = sizer.build_raw([Signal("s", "F", 1.0, stop_distance=10.0)], {"s": 1.0}, 500.0, state, [])
    audits = []
    assert sizer.finalize(book, _tier(), state, {}, {}, audits) == []
    assert any(a.rule == "min_lot_promotion_rejected" for a in audits)


def test_min_lot_promotion_respects_equity_risk_cap():
    """The promoted lot must NEVER breach the risk ceiling: at a small float
    one lot is genuinely too big and the honest answer stays no-trade."""
    state = _state_with({"F": 100.0}, equity=100_000.0,
                        kind=InstrumentKind.FUTURE, lot_size=65)
    sizer = SizingEngine(SizingConfig(min_lot_promotion=True),
                         CostModel(CostConfig()), 0.0)
    sig = Signal("s", "F", 1.0, stop_distance=10.0)     # lot risk 650 > 0.5% of 1e5
    book = sizer.build_raw([sig], {"s": 1.0}, 50.0, state, [])
    audits = []
    assert sizer.finalize(book, _tier(), state, {}, {}, audits) == []
    assert any(a.rule == "rounds_to_zero" for a in audits)
    assert not any(a.rule == "min_lot_promotion" for a in audits)


def test_min_lot_promotion_never_touches_spread_legs():
    """Promoting one leg of a pair would corrupt the hedge ratio — multi-leg
    groups are excluded from promotion and still drop whole."""
    state = _state_with({"A": 100.0, "B": 50.0}, equity=50_000_000.0,
                        kind=InstrumentKind.FUTURE, lot_size=500)
    sizer = SizingEngine(SizingConfig(min_lot_promotion=True),
                         CostModel(CostConfig()), 0.0)
    sig = Signal("mr", "A", 1.0, 5.0, legs=(LegSpec("A", 1.0), LegSpec("B", -0.01)))
    book = sizer.build_raw([sig], {"mr": 1.0}, 5000.0, state, [])
    audits = []
    assert sizer.finalize(book, _tier(), state, {}, {}, audits) == []
    assert not any(a.rule == "min_lot_promotion" for a in audits)


def test_bad_price_or_stop_skipped_with_audit():
    state = _state_with({"X": 100.0})
    audits = []
    book = _sizer().build_raw(
        [Signal("s", "MISSING", 1.0, 5.0)], {"s": 1.0}, 6000.0, state, audits)
    assert book.empty
    assert any(a.rule == "bad_price" for a in audits)



@pytest.mark.parametrize("stop", [0.0, -1.0, float("nan"), float("inf")])
def test_non_positive_or_non_finite_stop_is_rejected_not_floored(stop):
    """A zero stop used to be floored to 5 ticks: 84,000 shares (84% of
    equity) before any cap."""
    state = _state_with({"X": 100.0}, equity=10_000_000.0)
    audits = []
    book = _sizer().build_raw([Signal("s", "X", 1.0, stop)], {"s": 0.35}, 60_000.0, state, audits)
    assert book.empty
    assert [(a.rule, a.symbol) for a in audits] == [("bad_stop", "X")]


def test_positive_stop_below_the_tick_floor_is_still_floored():
    state = _state_with({"X": 100.0})
    book = _sizer().build_raw([Signal("s", "X", 1.0, 0.01)], {"s": 1.0}, 600.0, state, [])
    assert book.net_qty()["X"] == pytest.approx(600.0 / (5 * 0.05))


def test_pair_dropped_whole_when_one_leg_is_under_the_notional_floor():
    """X +60 (Rs6,000) / Y -36 (Rs3,600) against a Rs5,000 floor: the order
    diff would send X alone and leave the hedge unopened."""
    state = _state_with({"X": 100.0, "Y": 100.0})
    X, Y = state.price("X"), state.price("Y")
    sizer = _sizer(min_notional=5_000.0)
    sig = Signal("mr", "X", 1.0, 5.0, legs=(LegSpec("X", 1.0), LegSpec("Y", -0.6)), tag="X|Y")
    book = sizer.build_raw([sig], {"mr": 0.05}, 1_000_000 * 0.006, state, [])
    assert abs(book.net_qty()["X"]) * X > 5_000.0 > abs(book.net_qty()["Y"]) * Y
    audits = []
    assert sizer.finalize(book, _tier(band=0.1), state, {}, {}, audits) == []
    assert any(a.rule == "dust" and a.symbol == "Y" for a in audits)


def _futures_state(equity: float, price: float = 25_000.0):
    bars = {"NIFTY-FUT": make_hist(gbm(60, start=price, vol=0.0, seed=0))}
    insts = {"NIFTY-FUT": make_inst("NIFTY-FUT", kind=InstrumentKind.FUTURE, lot_size=65,
                                    sector="index", adv=250_000, margin_rate=0.125)}
    return make_state(bars, insts, equity=equity)


def _promoting_sizer() -> SizingEngine:
    return SizingEngine(SizingConfig(min_lot_promotion=True, promotion_max_risk_frac=0.005),
                        CostModel(CostConfig()), 5_000.0, 0.0001)


def test_min_lot_promotion_rejected_when_the_lot_breaches_a_cap():
    """Raw 63 units, the instrument cap cuts to 30 (0.46 lot); promoting to one
    65-unit lot would hold 54% of equity against a 25% cap."""
    E = 3_000_000.0
    state = _futures_state(E)
    sizer = _promoting_sizer()
    sig = Signal("trend", "NIFTY-FUT", 1.0, stop_distance=100.0, expected_edge_R=0.12)
    book = sizer.build_raw([sig], {"trend": 0.35}, E * 0.006, state, [])
    ctx = RiskContext(E, {"NIFTY-FUT": state.price("NIFTY-FUT")}, state.instruments,
                      ExposureConfig(), _tier(), [], None)
    audits = []
    apply_exposure_rules(book, ctx, audits)
    assert 0 < book.net_qty()["NIFTY-FUT"] < 65
    assert sizer.finalize(book, _tier(), state, {}, {}, audits, ctx=ctx) == []
    rejected = [a for a in audits if a.rule == "min_lot_promotion_rejected"]
    assert rejected and "per_instrument_cap" in rejected[0].detail
    assert not any(a.rule == "min_lot_promotion" for a in audits)


def test_min_lot_promotion_ceiling_scales_with_the_risk_scale():
    state = _state_with({"F": 100.0}, equity=50_000_000.0,
                        kind=InstrumentKind.FUTURE, lot_size=65)
    sizer = SizingEngine(SizingConfig(min_lot_promotion=True), CostModel(CostConfig()), 0.0)
    sig = Signal("s", "F", 1.0, stop_distance=10.0)  # lot risk 650
    caps = _caps(state)
    book = sizer.build_raw([sig], {"s": 1.0}, 500.0, state, [])
    assert [t.qty for t in sizer.finalize(book, _tier(), state, {}, {}, [], ctx=caps,
                                          risk_scale=0.01)] == [65]
    book = sizer.build_raw([sig], {"s": 1.0}, 500.0, state, [])
    audits = []
    # ceiling 0.5% * 5e7 * 0.002 = 500 < 650
    assert sizer.finalize(book, _tier(), state, {}, {}, audits, ctx=caps, risk_scale=0.002) == []
    assert any(a.rule == "min_lot_promotion_rejected" and "ceiling" in a.detail for a in audits)


def test_min_lot_promotion_takes_its_sign_from_the_signal():
    """A zero qty (regime risk_scaler 0) used to become a SHORT lot because
    the sign was read off the scaled qty."""
    state = _state_with({"F": 100.0}, equity=50_000_000.0,
                        kind=InstrumentKind.FUTURE, lot_size=65)
    sizer = SizingEngine(SizingConfig(min_lot_promotion=True), CostModel(CostConfig()), 0.0)
    sig = Signal("s", "F", 1.0, stop_distance=10.0)
    for flip in (lambda c: 0.0, lambda c: -abs(c.qty)):  # zero, and against the signal
        book = sizer.build_raw([sig], {"s": 1.0}, 500.0, state, [])
        book.components[0].qty = flip(book.components[0])
        audits = []
        assert sizer.finalize(book, _tier(), state, {}, {}, audits, ctx=_caps(state)) == []
        assert any(a.rule == "min_lot_promotion_rejected" and "signal side" in a.detail
                   for a in audits)


def test_final_targets_respect_caps_after_a_netting_group_is_dropped():
    """Factor +40% of E on X nets with a pair short -20% on X to 20%, under the
    25% cap; the pair's futures leg then rounds to zero lots and the pair is
    dropped, which used to leave X at 40%."""
    E = 1_000_000.0
    state = _state_with({"X": 100.0, "F": 50.0}, equity=E)
    X, F = state.price("X"), state.price("F")
    insts = {"X": make_inst("X", sector="banks"),
             "F": make_inst("F", kind=InstrumentKind.FUTURE, lot_size=500, sector="other")}
    state = make_state(state.bars, insts, equity=E)
    book = TargetBook()
    book.add_group([Component("X", 0.40 * E / X, "factor", "factor:X", 5.0, X)],
                   Signal("factor", "X", 1.0, 5.0, tag="X"))
    book.add_group([Component("X", -0.20 * E / X, "meanrev", "meanrev:X|F", 5.0, X, True),
                    Component("F", 300.0, "meanrev", "meanrev:X|F", 2.5, F, False)],
                   Signal("meanrev", "X", -1.0, 5.0, legs=(LegSpec("X", 1.0), LegSpec("F", -0.075)),
                          tag="X|F"))
    prices = {"X": X, "F": F}
    ctx = RiskContext(E, prices, insts, ExposureConfig(), _tier(), [], None)
    audits = []
    apply_exposure_rules(book, ctx, audits)
    assert audits == []  # the net passes every cap before rounding
    targets = _sizer(min_notional=5_000.0).finalize(book, _tier(), state, {}, {}, audits, ctx=ctx)
    net_x = sum(t.qty for t in targets if t.symbol == "X")
    assert 0 < net_x * X <= 0.25 * E
    assert {t.group_id for t in targets} == {"factor:X"}
    assert any(a.stage == "risk" and "per_instrument_cap" in a.detail for a in audits)



def _pair_and_single(state):
    book = TargetBook()
    pair = Signal("mr", "A", 1.0, 1.0, legs=(LegSpec("A", 1.0), LegSpec("B", -1.0)), tag="A|B")
    book.add_group([Component("A", 100.0, "mr", pair.group_id, 1.0, 100.0, is_parent=True),
                    Component("B", -100.0, "mr", pair.group_id, 1.0, 100.0)], pair)
    single = Signal("tr", "C", 1.0, 1.0, tag="C")
    book.add_group([Component("C", 50.0, "tr", single.group_id, 1.0, 100.0, is_parent=True)],
                   single)
    return book


def test_equity_shorts_off_drops_the_whole_pair_not_one_leg():
    """Live cannot carry a cash-equity short overnight. Refusing only the
    short order at the broker sent the pair's long leg unhedged; the sizer
    now drops the group whole and says why."""
    state = _state_with({"A": 100.0, "B": 100.0, "C": 100.0})
    sizer = SizingEngine(SizingConfig(allow_equity_shorts=False), CostModel(CostConfig()), 0.0)
    audits: list = []
    targets = sizer.finalize(_pair_and_single(state), _tier(), state, {}, {}, audits,
                             ctx=_caps(state))
    assert [(t.symbol, t.qty) for t in targets] == [("C", 50)]
    assert any(a.rule == "equity_short_blocked" and a.symbol == "B" for a in audits)


def test_equity_shorts_allowed_by_default_for_backtest_and_paper():
    state = _state_with({"A": 100.0, "B": 100.0, "C": 100.0})
    targets = _sizer().finalize(_pair_and_single(state), _tier(), state, {}, {}, [],
                                ctx=_caps(state))
    assert sorted((t.symbol, t.qty) for t in targets) == [("A", 100), ("B", -100), ("C", 50)]


def test_a_short_leg_netted_into_a_larger_long_stays():
    state = _state_with({"A": 100.0, "B": 100.0, "C": 100.0})
    book = _pair_and_single(state)
    extra = Signal("tr", "B", 1.0, 1.0, tag="B-long")      # B nets +50: long, fine
    book.add_group([Component("B", 150.0, "tr", extra.group_id, 1.0, 100.0, is_parent=True)],
                   extra)
    sizer = SizingEngine(SizingConfig(allow_equity_shorts=False), CostModel(CostConfig()), 0.0)
    targets = sizer.finalize(book, _tier(), state, {}, {}, [], ctx=_caps(state))
    net: dict[str, int] = {}
    for t in targets:
        net[t.symbol] = net.get(t.symbol, 0) + t.qty
    assert net == {"A": 100, "B": 50, "C": 50}
