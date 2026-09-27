import numpy as np
import pytest
from tests.conftest import make_inst

from quantsys.config.schema import ExposureConfig
from quantsys.core.types import ExecutionStyle, LegSpec, Signal
from quantsys.portfolio.book import Component, TargetBook
from quantsys.portfolio.tiers import TierState
from quantsys.risk.rules import RiskContext, apply_exposure_rules

E = 1_000_000.0


def _tier(lev=2.0, adv_cap_pct=0.02):
    return TierState("T", 0, 10, 4, ExecutionStyle.LIMIT_SMART, adv_cap_pct,
                     0.0, 0.2, lev)


def _ctx(insts, prices, cfg=None, tier=None, corr_syms=None, corr=None):
    return RiskContext(equity=E, prices=prices, instruments=insts,
                       cfg=cfg or ExposureConfig(), tier=tier or _tier(),
                       corr_symbols=corr_syms or [], corr=corr)


def _single(symbol, qty, price, strategy="s", gid=None) -> list[Component]:
    return [Component(symbol, qty, strategy, gid or f"g:{symbol}", 1.0, price)]


def test_per_instrument_cap_scales_to_exact_cap():
    book = TargetBook()
    sig = Signal("s", "X", 1.0, 1.0)
    book.add_group(_single("X", 5000.0, 100.0), sig)  # 500k = 50% of E
    ctx = _ctx({"X": make_inst("X")}, {"X": 100.0},
               cfg=ExposureConfig(per_instrument_frac=0.25))
    audits = []
    apply_exposure_rules(book, ctx, audits)
    assert abs(book.net_notional(ctx.prices, ctx.instruments)["X"]) == pytest.approx(0.25 * E)
    assert any(a.rule == "per_instrument_cap" for a in audits)


def test_pair_hedge_ratio_survives_caps():
    book = TargetBook()
    sig = Signal("mr", "A", 1.0, 1.0, legs=(LegSpec("A", 1.0), LegSpec("B", -2.0)))
    book.add_group([Component("A", 6000.0, "mr", "g", 1.0, 100.0),
                    Component("B", -24000.0, "mr", "g", 1.0, 50.0)], sig)
    insts = {"A": make_inst("A"), "B": make_inst("B")}
    prices = {"A": 100.0, "B": 50.0}
    apply_exposure_rules(book, _ctx(insts, prices), [])
    nq = book.net_qty()
    assert (nq["B"] * 50.0) / (nq["A"] * 100.0) == pytest.approx(-2.0, rel=1e-9)
    assert abs(nq["B"] * 50.0) <= 0.25 * E * (1 + 1e-9)


def test_sector_cap():
    book = TargetBook()
    for s in ("B1", "B2", "B3"):
        book.add_group(_single(s, 2400.0, 100.0), Signal("s", s, 1.0, 1.0))  # 240k each
    insts = {s: make_inst(s, sector="banks") for s in ("B1", "B2", "B3")}
    prices = dict.fromkeys(insts, 100.0)
    cfg = ExposureConfig(per_instrument_frac=0.25, sector_frac=0.50)
    audits = []
    apply_exposure_rules(book, _ctx(insts, prices, cfg=cfg), audits)
    nn = book.net_notional(prices, insts)
    assert sum(abs(v) for v in nn.values()) == pytest.approx(0.50 * E, rel=1e-9)
    assert any(a.rule == "sector_cap" for a in audits)


def test_corr_cluster_cap():
    book = TargetBook()
    for s in ("X", "Y"):
        book.add_group(_single(s, 3000.0, 100.0), Signal("s", s, 1.0, 1.0))  # 300k each
    insts = {s: make_inst(s, sector=s) for s in ("X", "Y")}  # distinct sectors
    prices = dict.fromkeys(insts, 100.0)
    corr = np.array([[1.0, 0.95], [0.95, 1.0]])
    cfg = ExposureConfig(per_instrument_frac=0.5, sector_frac=1.0,
                         corr_threshold=0.7, corr_cluster_frac=0.40)
    audits = []
    apply_exposure_rules(book, _ctx(insts, prices, cfg=cfg, corr_syms=["X", "Y"],
                                    corr=corr), audits)
    nn = book.net_notional(prices, insts)
    assert sum(abs(v) for v in nn.values()) == pytest.approx(0.40 * E, rel=1e-9)
    assert any(a.rule == "corr_cluster_cap" for a in audits)


def test_adv_cap_binds_on_quantity():
    book = TargetBook()
    book.add_group(_single("X", 50_000.0, 100.0), Signal("s", "X", 1.0, 1.0))
    insts = {"X": make_inst("X", adv=100_000)}
    cfg = ExposureConfig(per_instrument_frac=99.0, sector_frac=99.0, net_frac=99.0,
                         margin_util_cap=99.0)
    audits = []
    apply_exposure_rules(book, _ctx(insts, {"X": 100.0}, cfg=cfg,
                                    tier=_tier(lev=99.0, adv_cap_pct=0.02)), audits)
    assert book.net_qty()["X"] == pytest.approx(2000.0)  # 2% of 100k ADV
    assert any(a.rule == "adv_cap" for a in audits)


def test_gross_and_margin_caps_global():
    book = TargetBook()
    for s, q in (("X", 12_000.0), ("Y", -12_000.0)):
        book.add_group(_single(s, q, 100.0), Signal("s", s, 1.0, 1.0))  # 2.4m gross
    insts = {s: make_inst(s, sector=s, margin_rate=0.5) for s in ("X", "Y")}
    prices = dict.fromkeys(insts, 100.0)
    cfg = ExposureConfig(per_instrument_frac=99.0, sector_frac=99.0,
                         corr_cluster_frac=99.0, net_frac=99.0, margin_util_cap=0.60)
    audits = []
    apply_exposure_rules(book, _ctx(insts, prices, cfg=cfg, tier=_tier(lev=2.0)), audits)
    gross = book.gross(prices, insts)
    margin = 0.5 * gross
    assert gross <= 2.0 * E + 1e-6
    assert margin <= 0.60 * E + 1e-6
    assert any(a.rule == "gross_cap" for a in audits)
    assert any(a.rule == "margin_cap" for a in audits)


def test_net_cap():
    book = TargetBook()
    for s in ("X", "Y"):
        book.add_group(_single(s, 9000.0, 100.0), Signal("s", s, 1.0, 1.0))  # net 1.8m
    insts = {s: make_inst(s, sector=s) for s in ("X", "Y")}
    prices = dict.fromkeys(insts, 100.0)
    cfg = ExposureConfig(per_instrument_frac=99.0, sector_frac=99.0,
                         corr_cluster_frac=99.0, net_frac=1.5, margin_util_cap=99.0)
    apply_exposure_rules(book, _ctx(insts, prices, cfg=cfg, tier=_tier(lev=99.0)), [])
    assert abs(book.net(prices, insts)) <= 1.5 * E + 1e-6



def test_sector_cap_scales_an_intra_sector_pair_once():
    """A pair with both legs in one sector used to be scaled once per member
    symbol, ending at factor squared."""
    book = TargetBook()
    book.add_group([Component("A", 2000.0, "mr", "mr:A|B", 1.0, 100.0),
                    Component("B", -2000.0, "mr", "mr:A|B", 1.0, 100.0, False)],
                   Signal("mr", "A", 1.0, 1.0, legs=(LegSpec("A", 1.0), LegSpec("B", -1.0)),
                          tag="A|B"))
    book.add_group(_single("C", 2000.0, 100.0, "trend", "trend:C"), Signal("trend", "C", 1.0, 1.0))
    insts = {s: make_inst(s, sector="banks") for s in "ABC"}
    prices = dict.fromkeys(insts, 100.0)
    audits = []
    apply_exposure_rules(book, _ctx(insts, prices, cfg=ExposureConfig(sector_frac=0.30)), audits)
    nn = book.net_notional(prices, insts)
    assert sum(abs(v) for v in nn.values()) == pytest.approx(0.30 * E, rel=1e-9)
    assert nn == pytest.approx({"A": 100_000.0, "B": -100_000.0, "C": 100_000.0}, rel=1e-9)
    assert [a.rule for a in audits] == ["sector_cap"]


def test_corr_cluster_cap_scales_a_multi_member_group_once():
    book = TargetBook()
    book.add_group([Component("X", 2000.0, "mr", "mr:X|Y", 1.0, 100.0),
                    Component("Y", -2000.0, "mr", "mr:X|Y", 1.0, 100.0, False)],
                   Signal("mr", "X", 1.0, 1.0, legs=(LegSpec("X", 1.0), LegSpec("Y", -1.0)),
                          tag="X|Y"))
    book.add_group(_single("Z", 2000.0, 100.0, "trend", "trend:Z"), Signal("trend", "Z", 1.0, 1.0))
    insts = {s: make_inst(s, sector=s) for s in "XYZ"}
    prices = dict.fromkeys(insts, 100.0)
    corr = np.array([[1.0, -0.9, 0.9], [-0.9, 1.0, -0.9], [0.9, -0.9, 1.0]])
    cfg = ExposureConfig(per_instrument_frac=0.5, sector_frac=1.0, corr_threshold=0.7,
                         corr_cluster_frac=0.40)
    apply_exposure_rules(book, _ctx(insts, prices, cfg=cfg, corr_syms=["X", "Y", "Z"], corr=corr), [])
    nn = book.net_notional(prices, insts)
    assert sum(abs(v) for v in nn.values()) == pytest.approx(0.40 * E, rel=1e-9)
    assert nn["X"] == pytest.approx(-nn["Y"], rel=1e-12)


def test_apply_exposure_rules_reports_the_symbols_it_cut():
    book = TargetBook()
    book.add_group([Component("A", 4000.0, "mr", "mr:A|B", 1.0, 100.0),
                    Component("B", -1000.0, "mr", "mr:A|B", 1.0, 100.0, False)],
                   Signal("mr", "A", 1.0, 1.0, legs=(LegSpec("A", 1.0), LegSpec("B", -0.25)),
                          tag="A|B"))
    book.add_group(_single("C", 1000.0, 100.0, "trend", "trend:C"), Signal("trend", "C", 1.0, 1.0))
    insts = {s: make_inst(s, sector=s) for s in "ABC"}
    cut = apply_exposure_rules(book, _ctx(insts, dict.fromkeys(insts, 100.0)), [])
    assert cut == {"A", "B"}  # A breached the instrument cap, B is its hedge leg
