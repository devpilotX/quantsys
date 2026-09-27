"""Hedge-ratio integrity through lot rounding.

Fractional sizing derives every non-parent leg from the parent notional, so a
pair's hedge ratio is exact by construction. `finalize()` then rounds each leg
to whole lots *independently*, with floor division: and the legs do not round
by the same proportion. A pair intended at 7.0 / 2.4 lots ships as 7 / 2, which
is 17% under-hedged.

The only rounding guard before this was "drop the group if a leg rounds to
zero", which catches the degenerate case and misses the expensive one: a
market-neutral pair that quietly carries directional exposure, sized as though
it did not. The README's claim that "pair ratios are exact by construction and
survive every cap" holds for the caps; it did not hold for the rounding that
happens after them.
"""

from __future__ import annotations

import pytest
from tests.conftest import gbm, make_hist, make_inst, make_state

from quantsys.config.schema import CostConfig, SizingConfig
from quantsys.core.types import ExecutionStyle, LegSpec, Signal
from quantsys.costs import CostModel
from quantsys.portfolio.book import Component, TargetBook
from quantsys.portfolio.sizing import SizingEngine
from quantsys.portfolio.tiers import TierState


def _tier() -> TierState:
    return TierState("T", 0, 10, 4, ExecutionStyle.LIMIT_SMART, 1.0, 0.0, 0.2, 2.0)


def _sizer(**kw) -> SizingEngine:
    return SizingEngine(SizingConfig(**kw), CostModel(CostConfig()), 0.0)


def _state(prices: dict[str, float], lots: dict[str, int], equity=10_000_000.0):
    bars = {s: make_hist(gbm(60, start=p, vol=0.0001, seed=i))
            for i, (s, p) in enumerate(prices.items())}
    insts = {s: make_inst(s, lot_size=lots[s]) for s in prices}
    return make_state(bars, insts, equity=equity)


def _pair_book(state, qty_a: float, qty_b: float) -> TargetBook:
    """A two-leg group with no group_meta, so the cost gate is skipped and the
    test isolates rounding behaviour."""
    book = TargetBook()
    book.components.extend([
        Component("A", qty_a, "meanrev", "p1", stop_distance=1.0,
                  ref_price=state.price("A"), is_parent=True),
        Component("B", qty_b, "meanrev", "p1", stop_distance=1.0,
                  ref_price=state.price("B"), is_parent=False),
    ])
    return book


def _audit_kinds(audits) -> set[str]:
    return {a.rule for a in audits}


def test_material_ratio_drift_is_rejected_rather_than_shipped():
    """Legs at 7.0 and 2.4 lots: B rounds to 2, i.e. 17% under-hedged."""
    state = _state({"A": 100.0, "B": 100.0}, {"A": 1, "B": 1})
    # lot_size 1 with small quantities reproduces the same arithmetic as large
    # lots with few lots: 7.0 -> 7 exactly, 2.4 -> 2.
    book = _pair_book(state, qty_a=7.0, qty_b=2.4)

    audits: list = []
    targets = _sizer().finalize(book, _tier(), state, {}, {}, audits)

    assert "hedge_ratio_reject" in _audit_kinds(audits)
    assert targets == [], "an under-hedged pair was shipped"


def test_tolerable_ratio_drift_is_allowed_and_recorded():
    """Many lots per leg: rounding error is immaterial, so the trade proceeds,
    but the realised drift is still written to the audit trail."""
    state = _state({"A": 100.0, "B": 100.0}, {"A": 1, "B": 1})
    book = _pair_book(state, qty_a=1000.0, qty_b=500.4)

    audits: list = []
    targets = _sizer().finalize(book, _tier(), state, {}, {}, audits)

    kinds = _audit_kinds(audits)
    assert "hedge_ratio_drift" in kinds
    assert "hedge_ratio_reject" not in kinds
    assert {t.symbol for t in targets} == {"A", "B"}
    qty = {t.symbol: t.qty for t in targets}
    assert qty == {"A": 1000, "B": 500}


def test_drift_is_measured_against_the_intended_notional_ratio_not_quantity():
    """Legs at different prices: the invariant is about notional exposure.

    A ratio check on raw quantities would pass a badly hedged pair whenever the
    two legs' prices differ, which is the normal case.
    """
    state = _state({"A": 1000.0, "B": 10.0}, {"A": 1, "B": 1})
    # Intended: A notional 5 * 1000 = 5000, B notional 240 * 10 = 2400.
    # B rounds 240.0 -> 240 (no drift); A rounds 5.0 -> 5. Ratio preserved.
    book = _pair_book(state, qty_a=5.0, qty_b=240.0)
    audits: list = []
    targets = _sizer().finalize(book, _tier(), state, {}, {}, audits)
    assert "hedge_ratio_reject" not in _audit_kinds(audits)
    assert len(targets) == 2


def test_single_leg_groups_are_unaffected():
    """There is no ratio to preserve, so no drift check and no new rejection."""
    state = _state({"A": 100.0}, {"A": 1})
    book = TargetBook()
    book.components.append(
        Component("A", 7.9, "trend", "g1", stop_distance=1.0, ref_price=100.0, is_parent=True)
    )
    audits: list = []
    targets = _sizer().finalize(book, _tier(), state, {}, {}, audits)
    assert [t.qty for t in targets] == [7]
    assert "hedge_ratio_drift" not in _audit_kinds(audits)
    assert "hedge_ratio_reject" not in _audit_kinds(audits)


def test_a_leg_rounding_to_zero_is_still_dropped_first():
    """The pre-existing guard must keep priority: a zero leg is dropped as
    rounds_to_zero, not reported as a ratio problem."""
    state = _state({"A": 100.0, "B": 100.0}, {"A": 1, "B": 1})
    book = _pair_book(state, qty_a=10.0, qty_b=0.4)
    audits: list = []
    targets = _sizer().finalize(book, _tier(), state, {}, {}, audits)
    assert "rounds_to_zero" in _audit_kinds(audits)
    assert targets == []


def test_tolerance_is_configurable_and_a_wide_tolerance_admits_the_trade():
    """Confirms the guard is driven by config rather than a hardcoded constant,
    and that the rejection in the first test is caused by the tolerance."""
    state = _state({"A": 100.0, "B": 100.0}, {"A": 1, "B": 1})

    strict: list = []
    t_strict = _sizer(max_hedge_ratio_drift=0.01).finalize(
        _pair_book(state, 7.0, 2.4), _tier(), state, {}, {}, strict)

    loose: list = []
    t_loose = _sizer(max_hedge_ratio_drift=0.99).finalize(
        _pair_book(state, 7.0, 2.4), _tier(), state, {}, {}, loose)

    assert t_strict == []
    assert {t.symbol for t in t_loose} == {"A", "B"}


def test_real_pair_signal_survives_sizing_with_an_exact_ratio():
    """End-to-end through build_raw: a beta-hedged pair on liquid legs must not
    be rejected by the new guard, otherwise it would disable the sleeve."""
    state = _state({"A": 100.0, "B": 50.0}, {"A": 1, "B": 1})
    sig = Signal(
        "meanrev", "A", direction=1.0, stop_distance=2.0,
        legs=(LegSpec("A", 1.0), LegSpec("B", -0.8)),
    )
    sizer = _sizer()
    book = sizer.build_raw([sig], {"meanrev": 0.5}, 10_000_000 * 0.006, state, [])
    assume_two_legs = len(book.components)
    assert assume_two_legs == 2

    audits: list = []
    targets = sizer.finalize(book, _tier(), state, {}, {}, audits)

    assert "hedge_ratio_reject" not in _audit_kinds(audits), (
        "the guard rejected an ordinary beta-hedged pair; tolerance is too tight"
    )
    assert len(targets) == 2
    qty = {t.symbol: t.qty for t in targets}
    # Realised notional ratio must be within tolerance of the intended 0.8.
    realised = abs(qty["B"] * 50.0) / abs(qty["A"] * 100.0)
    assert realised == pytest.approx(0.8, rel=0.10)
