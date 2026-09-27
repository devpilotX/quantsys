"""Property-based tests for the risk and sizing invariants.

The README makes strong, absolute claims about this stack: every cap is
"monotone-shrink", multi-leg scaling is "group-joint" so "no cap can ever orphan
one leg of a hedge", and "pair ratios are exact by construction and survive
every cap". Those claims were covered by example-based tests, which verify the
inputs someone thought of. Hypothesis generates the ones nobody thought of,
which is where cap interactions actually break.

Each test states the invariant as an executable property. Where an invariant is
deliberately *not* absolute, that is asserted too: an untrue "always" is worse
than a documented exception, because it stops people checking.
"""

from __future__ import annotations

import math

import pytest
from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st
from tests.conftest import make_inst

from quantsys.core.types import InstrumentKind
from quantsys.portfolio.book import Component, TargetBook
from quantsys.portfolio.tiers import TierLadder

# Trading-hours CI budget: enough examples to find interaction bugs, few enough
# that the suite stays fast. deadline is off because the first example pays
# import and JIT warmup.
SETTINGS = settings(
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)

prices = st.floats(min_value=1.0, max_value=100_000.0, allow_nan=False, allow_infinity=False)
qtys = st.floats(min_value=-1e6, max_value=1e6, allow_nan=False, allow_infinity=False)
factors = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)
equities = st.floats(min_value=1e4, max_value=1e10, allow_nan=False, allow_infinity=False)


def _book(qty_by_symbol: dict[str, float], group_id: str = "g1") -> TargetBook:
    book = TargetBook()
    comps = [
        Component(symbol=s, qty=q, strategy="trend", group_id=group_id,
                  stop_distance=1.0, ref_price=100.0, is_parent=(i == 0))
        for i, (s, q) in enumerate(qty_by_symbol.items())
    ]
    book.components.extend(comps)
    return book


# --------------------------------------------------------------- scaling
@SETTINGS
@given(
    qty_a=qtys, qty_b=qtys,
    price_a=prices, price_b=prices,
    factor=factors,
)
def test_scale_group_never_increases_gross_exposure(qty_a, qty_b, price_a, price_b, factor):
    """A group scale by f in [0,1] must not increase gross notional.

    This is the property the whole ordered single pass of risk rules relies on:
    if any step could grow exposure, a later cap could re-violate an earlier one.
    """
    book = _book({"A": qty_a, "B": qty_b})
    instruments = {"A": make_inst("A"), "B": make_inst("B")}
    px = {"A": price_a, "B": price_b}

    before = book.gross(px, instruments)
    book.scale_group("g1", factor)
    after = book.gross(px, instruments)

    assert after <= before + 1e-6
    assert math.isfinite(after)


@SETTINGS
@given(qty_a=qtys, qty_b=qtys, factor=factors)
def test_scale_group_preserves_the_leg_ratio_exactly(qty_a, qty_b, factor):
    """Group-joint scaling is what makes hedge ratios survive caps.

    Verified on quantities rather than notionals so the property is about the
    scaling operation itself and not about price bookkeeping.
    """
    assume(abs(qty_a) > 1e-9 and abs(qty_b) > 1e-9)
    assume(factor > 1e-12)

    book = _book({"A": qty_a, "B": qty_b})
    ratio_before = book.components[0].qty / book.components[1].qty

    book.scale_group("g1", factor)
    ratio_after = book.components[0].qty / book.components[1].qty

    assert ratio_after == pytest.approx(ratio_before, rel=1e-9)


@SETTINGS
@given(
    qty_a=qtys, qty_b=qtys, qty_c=qtys,
    price_a=prices, price_b=prices,
    factor=factors,
)
def test_scale_symbol_scales_that_symbols_net_by_exactly_the_factor(
    qty_a, qty_b, qty_c, price_a, price_b, factor
):
    """The load-bearing claim behind every per-instrument and ADV cap.

    The cap computes factor = cap/current from the symbol's NET exposure and
    then trusts that scaling brings it to exactly the cap. That only holds if
    scaling every group touching the symbol scales its net by exactly `factor`,
    including when the symbol appears in more than one concurrent group, which
    is the case a single-sleeve test never reaches (a future held by the trend
    sleeve and used as a pair leg at the same time).
    """
    book = TargetBook()
    book.components.extend([
        Component("A", qty_a, "trend", "g1", 1.0, 100.0, True),
        Component("B", qty_b, "trend", "g1", 1.0, 100.0, False),
        # Second group that also touches A, with the opposite sign available.
        Component("A", qty_c, "meanrev", "g2", 1.0, 100.0, True),
    ])
    instruments = {"A": make_inst("A"), "B": make_inst("B")}
    px = {"A": price_a, "B": price_b}

    net_before = book.net_notional(px, instruments)["A"]
    # A's net is a sum of two contributions that may be large and nearly
    # opposite, so the achievable accuracy is set by the magnitude of the terms
    # rather than of the result. Comparing against a fixed absolute epsilon
    # would be testing float64 cancellation, not the scaling invariant.
    scale_of_terms = abs(qty_a * price_a) + abs(qty_c * price_a)
    tol = 1e-9 * max(scale_of_terms, 1.0)

    book.scale_symbol("A", factor)
    net_after = book.net_notional(px, instruments)["A"]

    assert net_after == pytest.approx(factor * net_before, abs=tol)


@SETTINGS
@given(qty_a=qtys, qty_b=qtys, f1=factors, f2=factors)
def test_repeated_scaling_is_order_independent_and_still_shrinks(qty_a, qty_b, f1, f2):
    """Two caps firing in either order must land on the same book.

    The risk pass applies caps in a fixed order; this asserts the result does
    not secretly depend on that order, which is what lets the caps be reasoned
    about independently.
    """
    a = _book({"A": qty_a, "B": qty_b})
    a.scale_group("g1", f1)
    a.scale_group("g1", f2)

    b = _book({"A": qty_a, "B": qty_b})
    b.scale_group("g1", f2)
    b.scale_group("g1", f1)

    for ca, cb in zip(a.components, b.components):
        assert ca.qty == pytest.approx(cb.qty, rel=1e-9, abs=1e-12)
        assert abs(ca.qty) <= abs(qty_a if ca.symbol == "A" else qty_b) + 1e-9


@SETTINGS
@given(qtys_in=st.lists(qtys, min_size=1, max_size=6), factor=factors)
def test_scale_all_shrinks_every_component_uniformly(qtys_in, factor):
    book = _book({f"S{i}": q for i, q in enumerate(qtys_in)})
    before = [c.qty for c in book.components]
    book.scale_all(factor)
    for b, c in zip(before, book.components):
        assert c.qty == pytest.approx(b * factor, rel=1e-9, abs=1e-12)
        assert abs(c.qty) <= abs(b) + 1e-9


@SETTINGS
@given(qty_a=qtys, qty_b=qtys)
def test_dropping_a_group_removes_every_leg_so_no_hedge_is_orphaned(qty_a, qty_b):
    """The strongest form of "no cap can orphan one leg of a hedge"."""
    book = _book({"A": qty_a, "B": qty_b})
    book.drop_group("g1")
    assert book.components == []
    assert book.empty
    assert "g1" not in book.group_meta


# ------------------------------------------------------------- tier ladder
@SETTINGS
@given(equity=equities)
def test_tier_resolution_is_total_and_finite_for_any_equity(small_cfg, equity):
    """No equity may produce a NaN cap or a missing tier.

    Interpolation divides by log(next.min_equity) - log(current.min_equity), so
    a NaN here would flow straight into adv_cap_pct, gross_leverage_cap and the
    cost-gate multiple.
    """
    ladder = TierLadder(small_cfg.tiers)
    tier = ladder.resolve(equity)

    assert tier.name
    for field in ("adv_cap_pct", "min_cost_multiple", "rebalance_band", "gross_leverage_cap"):
        v = getattr(tier, field)
        assert math.isfinite(v), f"{field} is not finite at equity={equity}"
        assert v > 0.0, f"{field} is not positive at equity={equity}"
    assert tier.max_instruments >= 1
    assert tier.max_strategies >= 1


@SETTINGS
@given(equity=equities)
def test_gross_leverage_cap_is_monotone_non_decreasing_in_equity(small_cfg, equity):
    """More capital may unlock more leverage but must never unlock less.

    A non-monotone ladder would mean a profitable day could tighten the book,
    which is the opposite of the documented design.
    """
    ladder = TierLadder(small_cfg.tiers)
    lo = TierLadder(small_cfg.tiers).resolve(equity)
    hi = ladder.resolve(equity * 2.0)
    assert hi.gross_leverage_cap >= lo.gross_leverage_cap - 1e-9
    assert hi.max_instruments >= lo.max_instruments


@SETTINGS
@given(equity=equities)
def test_resolve_is_deterministic_for_a_fresh_ladder(small_cfg, equity):
    """Same equity, same config, same answer: hysteresis state aside.

    Two fresh ladders must agree, otherwise a restart would re-size the book.
    """
    a = TierLadder(small_cfg.tiers).resolve(equity)
    b = TierLadder(small_cfg.tiers).resolve(equity)
    assert a.name == b.name
    assert a.gross_leverage_cap == b.gross_leverage_cap
    assert a.adv_cap_pct == b.adv_cap_pct


# ------------------------------------------------------- documented exception
def test_vol_targeter_is_the_one_scaler_allowed_to_grow_the_book(small_cfg):
    """Asserted explicitly because the README's blanket "all scalings are
    monotone-decreasing" is not true of vol targeting, by design.

    When the proposed book's volatility is below target, the scaler exceeds 1.0
    and increases exposure. That is correct behaviour, and it is exactly why the
    vol targeter must run BEFORE the exposure caps: so the caps still bind
    last. Pinning it here means a future reordering has to argue with a test
    rather than with a docstring.
    """
    cfg = small_cfg.vol_target
    assert cfg.scaler_max >= 1.0, (
        "vol targeting is expected to be able to scale up; if this ever becomes "
        "<= 1.0 the ordering constraint in the risk pass can be relaxed"
    )
    assert cfg.scaler_min >= 0.0
    assert cfg.scaler_min <= cfg.scaler_max


# ------------------------------------------------------------ cost model
@SETTINGS
@given(
    qty=st.integers(min_value=1, max_value=5_000_000),
    price=prices,
    sigma=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)
def test_costs_are_non_negative_and_finite_and_monotone_in_size(small_cfg, qty, price, sigma):
    """Every cost component must be >= 0 and finite, and a bigger order must
    never cost less than a smaller one at the same price."""
    from quantsys.costs import CostModel

    cm = CostModel(small_cfg.costs)
    inst = make_inst("X", kind=InstrumentKind.EQUITY, adv=1e7)

    small = cm.order_cost(inst, qty, price, is_buy=True, sigma_daily=sigma)
    big = cm.order_cost(inst, qty * 2, price, is_buy=True, sigma_daily=sigma)

    for cb in (small, big):
        for name in ("brokerage", "stt", "exchange_txn", "sebi", "stamp", "gst",
                     "slippage", "impact", "dp"):
            v = getattr(cb, name)
            assert math.isfinite(v), f"{name} not finite"
            assert v >= 0.0, f"{name} negative"
        assert cb.total >= 0.0

    assert big.total >= small.total - 1e-9


@SETTINGS
@given(
    qty=st.integers(min_value=1, max_value=1_000_000),
    price=prices,
    sigma=st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False),
)
def test_round_trip_costs_at_least_as_much_as_one_leg(small_cfg, qty, price, sigma):
    """The cost gate compares edge against round_trip; if round_trip could come
    in below a single order's cost the gate would let through trades that cannot
    pay for their own exit."""
    from quantsys.costs import CostModel

    cm = CostModel(small_cfg.costs)
    inst = make_inst("X", kind=InstrumentKind.EQUITY, adv=1e7)

    buy = cm.order_cost(inst, qty, price, is_buy=True, sigma_daily=sigma)
    rt = cm.round_trip(inst, qty, price, sigma_daily=sigma)

    assert math.isfinite(rt)
    assert rt >= buy.total - 1e-9
