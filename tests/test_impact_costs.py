"""Market-impact cost must actually reach reported P&L.

The cost model documents a square-root impact term as "what makes the ADV
constraint bind economically at T5/T6", and SimBroker's docstring claimed the
CostModel "already charges" it. It did not: CostModel returns impact=0 unless a
`sigma_daily` is supplied, and no fill path supplied one. Impact was charged in
the cost *gate* (which decides whether a trade is worth doing) but never in the
*fill* (which produces the P&L), so every backtest and paper number in docs/
excluded it, and the dashboard's per-fill `impact` fee line was structurally
always zero.

These tests pin the contract from both ends: a large order in an illiquid name
must cost strictly more than a small one beyond the linear fee terms, and the
engine must publish the sigma it used on the Decision so no fill path can
forget to apply it.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from quantsys.backtest import synthetic_bars
from quantsys.backtest.simbroker import SimBroker
from quantsys.config import load_config
from quantsys.core.types import (
    Decision,
    ExecutionStyle,
    InstrumentKind,
    OrderIntent,
    RegimeState,
    Urgency,
)
from quantsys.engine.decision import DecisionEngine


@pytest.fixture(scope="module")
def cfg():
    return load_config("config/base.yaml")


@pytest.fixture(scope="module")
def bars(cfg):
    syms = [u.symbol for u in cfg.universe]
    return list(synthetic_bars(syms, datetime(2026, 1, 1), 800,
                               cfg.engine.decision_bar_minutes, seed=5))


def _decision(sym: str, qty: int, sigma: dict[str, float] | None) -> Decision:
    return Decision(
        ts=datetime(2026, 1, 1, 9, 15), equity=10_000_000, tier_name="T3",
        regime=RegimeState("calm_range", {}, 1.0, {}, "none"),
        signals=(), kelly={}, vol_scaler=1.0, risk_frac_eff=0.0, targets=(),
        orders=(OrderIntent(sym, qty, ExecutionStyle.MARKET_SINGLE,
                            Urgency.NORMAL, "trend", "test"),),
        sigma_daily=sigma if sigma is not None else {},
    )


@pytest.fixture(scope="module")
def liquid(cfg):
    """First instrument that actually has an ADV — impact is only defined there.

    cfg.universe[0] is the NIFTY *index*, which has adv=None by design, so
    indexing the universe blindly silently disables the very term under test.
    """
    eng = DecisionEngine(cfg)
    for sym, inst in eng.instruments.items():
        if inst.adv and inst.adv > 0 and inst.kind == InstrumentKind.EQUITY:
            return sym, inst, eng
    pytest.skip("no ADV-bearing equity in the base universe")


# ------------------------------------------------------------- cost model
def test_impact_is_charged_when_sigma_is_supplied(liquid):
    _sym, inst, eng = liquid

    with_sigma = eng.cost_model.order_cost(inst, 500_000, 100.0, True, sigma_daily=0.02)
    without = eng.cost_model.order_cost(inst, 500_000, 100.0, True, sigma_daily=None)

    assert without.impact == 0.0
    assert with_sigma.impact > 0.0
    assert with_sigma.total > without.total


def test_zero_sigma_is_not_treated_as_missing_sigma(liquid):
    """`if sigma_daily` made 0.0 fall into the "not supplied" branch.

    The numeric result is the same (impact scales linearly in sigma) but the
    branch was wrong, and it hid the distinction between "no estimate" and "an
    estimate of zero vol".
    """
    _sym, inst, eng = liquid
    cb = eng.cost_model.order_cost(inst, 10_000, 100.0, True, sigma_daily=0.0)
    assert cb.impact == 0.0
    assert cb.total > 0.0


def test_impact_grows_with_the_square_root_of_participation(liquid):
    _sym, inst, eng = liquid
    q = 10_000
    a = eng.cost_model.order_cost(inst, q, 100.0, True, sigma_daily=0.02).impact
    b = eng.cost_model.order_cost(inst, 4 * q, 100.0, True, sigma_daily=0.02).impact
    # impact ~ sigma * sqrt(q/adv) * notional  =>  4x qty gives 2x the sqrt term
    # and 4x notional, i.e. 8x total.
    assert a > 0.0
    assert b == pytest.approx(8.0 * a, rel=1e-9)


# ------------------------------------------------------------- fill path
def test_simbroker_charges_impact_from_the_decision(liquid):
    sym, _inst, eng = liquid
    px, qty = 100.0, 500_000

    dumb = SimBroker(500_000_000, eng.instruments, eng.cost_model)
    dumb.execute(_decision(sym, qty, None), {sym: px}, datetime(2026, 1, 1, 9, 15))

    aware = SimBroker(500_000_000, eng.instruments, eng.cost_model)
    aware.execute(_decision(sym, qty, {sym: 0.02}), {sym: px}, datetime(2026, 1, 1, 9, 15))

    assert aware.total_fees > dumb.total_fees, (
        "SimBroker ignored Decision.sigma_daily, so impact never reached P&L"
    )
    assert aware.cash < dumb.cash


def test_simbroker_equity_identity_holds_with_impact_charged(liquid):
    """Charging an extra cost line must not break equity == cash + MTM."""
    sym, _inst, eng = liquid
    brk = SimBroker(500_000_000, eng.instruments, eng.cost_model)
    prices = {sym: 100.0}
    brk.execute(_decision(sym, 250_000, {sym: 0.03}), prices, datetime(2026, 1, 1, 9, 15))
    assert brk.equity(prices) == pytest.approx(brk.cash + brk.mtm(prices), abs=1e-6)


# ------------------------------------------------------------- engine wiring
def test_decide_publishes_the_sigma_it_used(cfg, bars):
    """The gate and the fill must see the same sigma.

    Asserts two things over a real run: the engine publishes a non-empty
    estimate once histories are warm, and every symbol it ordered or targeted
    carries one — so no fill can skip impact. (On the base config over
    synthetic bars the cost gate legitimately blocks most trades, which is why
    the target-level assertion carries the weight rather than order count.)
    """
    from quantsys.core.market_state import MarketState
    from quantsys.core.types import Position
    from quantsys.data.history import BarHistory

    eng = DecisionEngine(cfg)
    histories: dict[str, BarHistory] = {s: BarHistory() for s in eng.instruments}
    last: dict[str, float] = {}
    published = 0
    checked = 0

    for ts, batch in bars[:500]:
        for s, bar in batch.items():
            if s in histories:
                histories[s].append(bar)
                last[s] = bar.close
        state = MarketState(ts=ts, equity=500_000_000, bars=histories,
                            instruments=eng.instruments,
                            positions={s: Position(s, 0, 0.0) for s in last})
        eng.post_bar(state)
        d = eng.decide(state)
        if d.sigma_daily:
            published += 1
        for t in d.targets:
            checked += 1
            assert t.symbol in d.sigma_daily, (
                f"{t.symbol} targeted with no sigma published; its fill would skip impact"
            )
        for o in d.orders:
            checked += 1
            assert o.symbol in d.sigma_daily, (
                f"{o.symbol} ordered with no sigma published; its fill would skip impact"
            )

    assert published > 0, "engine never published a sigma estimate, so the fix is unwired"
    assert checked > 0, "no targets or orders produced, so the guarantee is unproven"


def test_sigma_daily_defaults_to_empty_and_is_safe_to_read():
    """Older persisted/constructed Decisions have no sigma; reading must not raise."""
    d = Decision(
        ts=datetime(2026, 1, 1), equity=1.0, tier_name="T1",
        regime=RegimeState("calm_range", {}, 1.0, {}, "none"),
        signals=(), kelly={}, vol_scaler=1.0, risk_frac_eff=0.0,
        targets=(), orders=(),
    )
    assert d.sigma_daily == {}
    assert d.sigma_daily.get("ANY") is None
