import pytest
from tests.conftest import make_inst

from quantsys.config.schema import CostConfig
from quantsys.core.types import InstrumentKind
from quantsys.costs import CostModel


@pytest.fixture
def cm() -> CostModel:
    return CostModel(CostConfig())


def test_future_stt_sell_side_only(cm):
    fut = make_inst("NIFTY-FUT", kind=InstrumentKind.FUTURE, lot_size=65)
    buy = cm.order_cost(fut, 65, 25000.0, is_buy=True)
    sell = cm.order_cost(fut, 65, 25000.0, is_buy=False)
    notional = 65 * 25000.0
    assert buy.stt == 0.0
    assert sell.stt == pytest.approx(0.0005 * notional)  # Budget-2026 rate
    assert buy.stamp == pytest.approx(0.00002 * notional)
    assert sell.stamp == 0.0


def test_fno_brokerage_is_a_flat_fee_per_order(cm):
    """Angel One charges Rs 20 per executed F&O order, whatever its size."""
    fut = make_inst("F", kind=InstrumentKind.FUTURE)
    assert cm.order_cost(fut, 1000, 1000.0, is_buy=True).brokerage == 20.0
    assert cm.order_cost(fut, 1, 100.0, is_buy=True).brokerage == 20.0


@pytest.mark.parametrize("delivery", [True, False])
@pytest.mark.parametrize("notional,expected", [
    (1_000_000.0, 20.0),     # 0.1% is Rs 1,000: capped at Rs 20
    (10_000.0, 10.0),        # 0.1%
    (1_000.0, 5.0),          # 0.1% is Rs 1: the Rs 5 minimum
])
def test_equity_brokerage_follows_the_published_schedule(cm, delivery, notional, expected):
    """min(Rs 20, 0.1%) with a Rs 5 minimum, for delivery and intraday alike.
    Delivery was modelled as free; Angel One has charged it since 1 Nov 2024,
    so every delivery trade the engine sized looked cheaper than it was."""
    eq = make_inst("RELIANCE")
    c = cm.order_cost(eq, 100, notional / 100, is_buy=True, delivery=delivery)
    assert c.brokerage == pytest.approx(expected)


def test_a_fixed_delivery_fee_can_still_be_configured():
    free = CostModel(CostConfig(brokerage_delivery_flat=0.0))
    eq = make_inst("RELIANCE")
    assert free.order_cost(eq, 100, 1500.0, is_buy=True, delivery=True).brokerage == 0.0


def test_gst_base(cm):
    fut = make_inst("F", kind=InstrumentKind.FUTURE)
    c = cm.order_cost(fut, 100, 1000.0, is_buy=True)
    assert c.gst == pytest.approx(0.18 * (c.brokerage + c.exchange_txn + c.sebi))


def test_equity_delivery_vs_intraday(cm):
    eq = make_inst("RELIANCE")
    deliv = cm.order_cost(eq, 100, 1500.0, is_buy=True, delivery=True)
    intra = cm.order_cost(eq, 100, 1500.0, is_buy=True, delivery=False)
    assert deliv.brokerage == intra.brokerage == 20.0  # same schedule, both capped
    assert deliv.stt == pytest.approx(0.001 * 150000.0)  # both sides on delivery
    assert intra.stt == 0.0  # intraday STT is sell-side only


def test_impact_scales_with_participation(cm):
    eq = make_inst("X", adv=1_000_000)
    small = cm.order_cost(eq, 1_000, 100.0, is_buy=True, sigma_daily=0.02)
    big = cm.order_cost(eq, 100_000, 100.0, is_buy=True, sigma_daily=0.02)
    assert big.impact / big.brokerage > 0
    assert (big.impact / (100_000 * 100)) > (small.impact / (1_000 * 100))


def test_round_trip_positive_and_sane(cm):
    fut = make_inst("F", kind=InstrumentKind.FUTURE, lot_size=65)
    rt = cm.round_trip(fut, 65, 25000.0)
    notional = 65 * 25000.0
    assert 0 < rt < 0.01 * notional  # all-in round trip well under 1%
