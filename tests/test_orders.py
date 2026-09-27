from tests.conftest import make_inst

from quantsys.core.types import ExecutionStyle, Position, TargetPosition, Urgency
from quantsys.engine.orders import diff_orders
from quantsys.portfolio.tiers import TierState


def _tier(band=0.2, style=ExecutionStyle.LIMIT_SMART):
    return TierState("T", 0, 10, 4, style, 0.02, 0.0, band, 2.0)


INSTS = {"X": make_inst("X"), "Y": make_inst("Y", kind="FUTURE", lot_size=65)}
PRICES = {"X": 100.0, "Y": 200.0}


def _tp(sym, qty):
    return TargetPosition(sym, qty, "s", f"g:{sym}", 1.0, PRICES[sym])


def test_entry_exit_rebalance():
    orders = diff_orders([_tp("X", 300)], {}, INSTS, PRICES, _tier(), 0.0)
    assert orders[0].qty_delta == 300 and orders[0].reason == "entry"
    orders2 = diff_orders([], {"X": Position("X", 300)}, INSTS, PRICES, _tier(), 0.0)
    assert orders2[0].qty_delta == -300 and orders2[0].reason == "exit"
    orders3 = diff_orders([_tp("X", 400)], {"X": Position("X", 300)}, INSTS, PRICES,
                          _tier(band=0.1), 0.0)
    assert orders3[0].qty_delta == 100 and orders3[0].reason == "rebalance"


def test_rebalance_band_suppresses_churn():
    # delta 40 < band 0.2 * target 300 = 60 -> skip
    orders = diff_orders([_tp("X", 300)], {"X": Position("X", 260)}, INSTS, PRICES,
                         _tier(band=0.2), 0.0)
    assert orders == []
    # full exit ignores the band
    orders2 = diff_orders([], {"X": Position("X", 10)}, INSTS, PRICES, _tier(), 0.0)
    assert len(orders2) == 1


def test_min_notional_suppresses_dribbles():
    orders = diff_orders([_tp("X", 1000)], {"X": Position("X", 700)}, INSTS, PRICES,
                         _tier(band=0.01), min_order_notional=50_000.0)
    assert orders == []  # 300 * 100 = 30k < 50k


def test_kill_flattens_at_market():
    pos = {"X": Position("X", 300), "Y": Position("Y", -130)}
    orders = diff_orders([_tp("X", 300)], pos, INSTS, PRICES, _tier(), 0.0, kill=True)
    assert {o.symbol: o.qty_delta for o in orders} == {"X": -300, "Y": 130}
    assert all(o.urgency == Urgency.KILL and o.style == ExecutionStyle.MARKET_SINGLE
               for o in orders)


def test_risk_reducing_overrides_band_and_style():
    orders = diff_orders([], {"X": Position("X", 300)}, INSTS, PRICES, _tier(), 0.0,
                         risk_reducing_symbols={"X"})
    assert orders[0].urgency == Urgency.RISK_REDUCING
    assert orders[0].style == ExecutionStyle.MARKET_SINGLE



# ------------------------------------------------ cap-driven reductions
_EQ = {"X": make_inst("X"), "Y": make_inst("Y")}
_PX = {"X": 100.0, "Y": 100.0}


def _pair(qx: int, qy: int, gid: str = "mr:X|Y") -> list[TargetPosition]:
    return [TargetPosition("X", qx, "mr", gid, 5.0, 100.0),
            TargetPosition("Y", qy, "mr", gid, 5.0, 100.0)]


def test_cap_driven_reduction_passes_the_band():
    """Held 33.7% of equity against a 25% cap: the cut is inside the T1 band
    (0.35 * target) and used to be suppressed as churn."""
    cap_qty, held = 2500, 3366
    tgt = [TargetPosition("X", cap_qty, "trend", "trend:X", 2.0, 100.0)]
    pos = {"X": Position("X", held, 90.0)}
    assert diff_orders(tgt, pos, _EQ, _PX, _tier(band=0.35), 5000.0) == []  # ordinary rebalance
    orders = diff_orders(tgt, pos, _EQ, _PX, _tier(band=0.35), 5000.0,
                         cap_reducing_symbols={"X"})
    assert [(o.symbol, o.qty_delta, o.urgency) for o in orders] == [("X", cap_qty - held, Urgency.NORMAL)]


def test_cap_driven_reduction_still_respects_the_dust_floor():
    """A cut worth less than the dust floor stays unsent. Waiving the floor
    for cap-driven cuts re-opened one-share dribble orders during every
    drawdown slide, the churn the equity-scaled floor exists to stop."""
    tgt = [TargetPosition("X", 2500, "trend", "trend:X", 2.0, 100.0)]
    pos = {"X": Position("X", 2540, 90.0)}          # the cut is Rs 4,000
    assert diff_orders(tgt, pos, _EQ, _PX, _tier(band=0.35), 5000.0,
                       cap_reducing_symbols={"X"}) == []
    orders = diff_orders(tgt, pos, _EQ, _PX, _tier(band=0.35), 3000.0,
                         cap_reducing_symbols={"X"})
    assert [(o.symbol, o.qty_delta) for o in orders] == [("X", -40)]


def test_cap_reducing_flag_does_not_exempt_increases():
    tgt = [TargetPosition("X", 2100, "trend", "trend:X", 2.0, 100.0)]
    pos = {"X": Position("X", 2000, 90.0)}
    assert diff_orders(tgt, pos, _EQ, _PX, _tier(band=0.35), 0.0, cap_reducing_symbols={"X"}) == []


def test_cap_driven_reduction_still_needs_one_lot():
    insts = {"F": make_inst("F", kind="FUTURE", lot_size=65)}
    tgt = [TargetPosition("F", 65, "trend", "trend:F", 2.0, 200.0)]
    pos = {"F": Position("F", 100, 200.0)}  # off-lot position: the cut is 35 < one lot
    assert diff_orders(tgt, pos, insts, {"F": 200.0}, _tier(band=0.35), 0.0,
                       cap_reducing_symbols={"F"}) == []


# ------------------------------------------------------ multi-leg groups
def test_new_pair_legs_go_out_together_or_not_at_all():
    orders = diff_orders(_pair(60, -36), {}, _EQ, _PX, _tier(band=0.1), 5000.0)
    assert {o.symbol for o in orders} in (set(), {"X", "Y"})


def test_pair_rebalance_moves_both_legs_or_neither():
    """X 60 -> 100 clears the band, Y -48 -> -60 does not: sending X alone
    breaks the hedge ratio."""
    pos = {"X": Position("X", 60), "Y": Position("Y", -48)}
    orders = diff_orders(_pair(100, -60), pos, _EQ, _PX, _tier(band=0.25), 1000.0)
    assert {(o.symbol, o.qty_delta) for o in orders} in (set(), {("X", 40), ("Y", -12)})


def test_pair_inside_the_band_on_both_legs_sends_nothing():
    pos = {"X": Position("X", 98), "Y": Position("Y", -59)}
    assert diff_orders(_pair(100, -60), pos, _EQ, _PX, _tier(band=0.25), 0.0) == []


def test_single_leg_groups_keep_per_symbol_bands():
    tgts = [TargetPosition("X", 100, "trend", "trend:X", 1.0, 100.0),
            TargetPosition("Y", 60, "trend", "trend:Y", 1.0, 100.0)]
    pos = {"X": Position("X", 60), "Y": Position("Y", 50)}
    orders = diff_orders(tgts, pos, _EQ, _PX, _tier(band=0.25), 0.0)
    assert [(o.symbol, o.qty_delta) for o in orders] == [("X", 40)]
