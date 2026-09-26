"""Position sizing: signals -> fractional target quantities -> (after vol
targeting and risk caps) lot-rounded integer targets with a cost gate.

Per-signal risk budget:
    risk_i = E * base_risk_frac * f_strategy * conviction_share_i
    qty_parent = sign(direction) * risk_i / (stop_distance * point_value)
Non-parent legs derive from the parent notional via their notional_ratio, so
hedge ratios are exact by construction. The drawdown throttle and the regime
risk scaler are applied by the engine after vol targeting (scale_all).

The cost gate (binds hard at T1/T2): a NEW trade group must clear
    expected_edge_R * risk_rupees >= tier.min_cost_multiple * round_trip_cost.
Existing positions whose signal persists are not gated (gating a held position
would force-pay the exit cost the gate is trying to avoid). Equity costs are
gated at delivery rates — worst case, capital-preservation bias.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from quantsys.config.schema import SizingConfig
from quantsys.core.market_state import MarketState
from quantsys.core.types import (
    AuditEvent,
    InstrumentKind,
    Position,
    Signal,
    TargetPosition,
    Urgency,
)
from quantsys.costs import CostModel
from quantsys.portfolio.book import Component, TargetBook
from quantsys.portfolio.tiers import TierState
from quantsys.risk.rules import CapBreach, RiskContext, cap_breaches


class SizingEngine:
    def __init__(self, cfg: SizingConfig, cost_model: CostModel,
                 min_order_notional: float, min_order_frac: float = 0.0):
        self.cfg = cfg
        self.cost_model = cost_model
        self.min_order_notional = min_order_notional
        self.min_order_frac = min_order_frac

    def effective_min_notional(self, equity: float) -> float:
        """Dust floor scaled to the book: max(flat floor, frac * equity)."""
        return max(self.min_order_notional, self.min_order_frac * max(equity, 0.0))

    # ------------------------------------------------------------ raw build
    def build_raw(
        self,
        signals: list[Signal],
        kelly_f: dict[str, float],
        risk_amount_base: float,   # E * base_risk_frac; the engine throttles after vol targeting
        state: MarketState,
        audits: list[AuditEvent],
    ) -> TargetBook:
        book = TargetBook()
        by_strategy: dict[str, list[Signal]] = {}
        for sig in signals:
            by_strategy.setdefault(sig.strategy, []).append(sig)

        for strat in sorted(by_strategy):
            f_s = kelly_f.get(strat, 0.0)
            if f_s <= 0 or risk_amount_base <= 0:
                continue
            sigs = sorted(by_strategy[strat], key=lambda s: (-abs(s.direction), s.group_id))
            sigs = sigs[: self.cfg.max_signals_per_strategy]
            total_conv = sum(abs(s.direction) for s in sigs)
            for sig in sigs:
                share = (
                    abs(sig.direction) / total_conv
                    if self.cfg.conviction_weighting and total_conv > 0
                    else 1.0 / len(sigs)
                )
                comps = self._size_group(sig, risk_amount_base * f_s * share, state, audits)
                if comps:
                    book.add_group(comps, sig)
        return book

    def _size_group(
        self, sig: Signal, risk_amt: float, state: MarketState, audits: list[AuditEvent]
    ) -> list[Component] | None:
        parent_inst = state.instruments.get(sig.symbol)
        p_parent = state.price(sig.symbol)
        if parent_inst is None or not (math.isfinite(p_parent) and p_parent > 0):
            audits.append(AuditEvent("sizing", "bad_price", sig.group_id, symbol=sig.symbol))
            return None
        # A stop that is not a positive finite distance is a strategy bug, not
        # a tight stop: flooring it to the tick minimum sized a zero stop at
        # 84% of equity. Only a genuine positive stop is floored.
        if not (math.isfinite(sig.stop_distance) and sig.stop_distance > 0):
            audits.append(AuditEvent("sizing", "bad_stop", sig.group_id, symbol=sig.symbol))
            return None
        stop_d = max(sig.stop_distance, self.cfg.atr_min_stop_ticks * parent_inst.tick_size)

        sgn = 1.0 if sig.direction > 0 else -1.0
        qty_parent = sgn * risk_amt / (stop_d * parent_inst.point_value)
        parent_notional = abs(qty_parent) * p_parent * parent_inst.point_value

        comps: list[Component] = []
        for leg in sig.resolved_legs():
            inst = state.instruments.get(leg.symbol)
            p = state.price(leg.symbol)
            if inst is None or not (math.isfinite(p) and p > 0):
                audits.append(AuditEvent("sizing", "bad_leg", sig.group_id, symbol=leg.symbol))
                return None
            leg_qty = sgn * math.copysign(1.0, leg.notional_ratio) * (
                parent_notional * abs(leg.notional_ratio) / (p * inst.point_value)
            )
            comps.append(
                Component(
                    symbol=leg.symbol,
                    qty=leg_qty,
                    strategy=sig.strategy,
                    group_id=sig.group_id,
                    stop_distance=stop_d * p / p_parent,  # proportional backstop
                    ref_price=p,
                    is_parent=(leg.symbol == sig.symbol and leg.notional_ratio == 1.0),
                )
            )
        return comps

    # -------------------------------------------------------------- finalize
    def finalize(
        self,
        book: TargetBook,
        tier: TierState,
        state: MarketState,
        positions: dict[str, Position],
        sigma_daily: dict[str, float],
        audits: list[AuditEvent],
        *,
        ctx: RiskContext | None = None,
        risk_scale: float = 1.0,
        cut: set[str] | None = None,
    ) -> list[TargetPosition]:
        """Lot-round every group and drop what cannot trade, then re-verify caps.

        A group is dropped whole if any leg rounds to zero lots, any leg is
        under the notional floor, or a new trade fails the cost gate. With
        `ctx` every exposure cap is then re-checked on the rounded targets,
        because dropping a group that offset another can leave a symbol above
        a cap its net passed. A breach shrinks the groups touching it jointly
        in whole lots, re-applying the drop rules, until no cap is breached;
        the symbols shrunk or dropped are added to `cut`. Min-lot promotion
        runs last, against the clean book. Without `ctx` no cap is re-checked
        and no lot is promoted, since a promotion grows a position and must be
        verified; the decision engine always passes it.

        risk_scale is the drawdown throttle times the regime risk scaler the
        book was sized with. It scales the promotion risk ceiling.
        """
        groups = book.group_components()
        kept: dict[str, list[tuple[Component, int]]] = {}
        candidates: list[str] = []
        for gid in sorted(groups):
            comps = groups[gid]
            rounded = self._round(comps, 1.0, state)
            zero = next((c for c, q in rounded if q == 0), None)
            if zero is not None:
                if self._promotion_candidate(comps, state):
                    candidates.append(gid)
                else:
                    audits.append(AuditEvent("sizing", "rounds_to_zero", gid, symbol=zero.symbol,
                                             before=zero.qty, after=0.0))
                continue
            if self._tradeable(gid, rounded, book.group_meta.get(gid), tier, state, positions,
                               sigma_daily, audits):
                kept[gid] = rounded

        if not self.cfg.allow_equity_shorts:
            self._drop_equity_shorts(kept, state, audits, cut)
        if ctx is not None:
            self._enforce_caps(groups, kept, book, tier, state, positions, sigma_daily, ctx,
                               audits, cut)
        for gid in candidates:
            self._promote(gid, groups[gid][0], book.group_meta.get(gid), kept, tier, state,
                          positions, sigma_daily, ctx, risk_scale, audits)

        return [
            TargetPosition(symbol=c.symbol, qty=q, strategy=c.strategy, group_id=gid,
                           stop_distance=c.stop_distance, ref_price=c.ref_price,
                           urgency=Urgency.NORMAL)
            for gid in sorted(kept) for c, q in kept[gid]
        ]

    @staticmethod
    def _round(comps: list[Component], scale: float,
               state: MarketState) -> list[tuple[Component, int]]:
        out: list[tuple[Component, int]] = []
        for c in comps:
            lot = max(state.instruments[c.symbol].lot_size, 1)
            out.append((c, int(abs(c.qty) * scale // lot) * lot * (1 if c.qty > 0 else -1)))
        return out

    def _tradeable(self, gid: str, rounded: list[tuple[Component, int]], sig: Signal | None,
                   tier: TierState, state: MarketState, positions: dict[str, Position],
                   sigma_daily: dict[str, float], audits: list[AuditEvent]) -> bool:
        """Every leg clears the notional floor, and a new trade clears the cost
        gate. diff_orders skips a sub-floor order, which would open the other
        legs unhedged, so one sub-floor leg drops the whole group."""
        floor = self.effective_min_notional(state.equity)
        for c, q in rounded:
            leg = abs(q) * state.price(c.symbol) * state.instruments[c.symbol].point_value
            if leg < floor:
                audits.append(AuditEvent("sizing", "dust", gid, symbol=c.symbol,
                                         before=leg, after=0.0))
                return False

        if self.cfg.enforce_cost_gate and sig is not None and self._is_new_trade(rounded, positions):
            parent = next(((c, q) for c, q in rounded if c.is_parent), rounded[0])
            c_p, q_p = parent
            risk_rupees = abs(q_p) * c_p.stop_distance * state.instruments[c_p.symbol].point_value
            edge_rupees = sig.expected_edge_R * risk_rupees
            rt = 0.0
            for c, q in rounded:
                inst = state.instruments[c.symbol]
                rt += self.cost_model.round_trip(
                    inst, q, state.price(c.symbol),
                    delivery=(inst.kind == InstrumentKind.EQUITY),
                    sigma_daily=sigma_daily.get(c.symbol),
                )
            if edge_rupees < tier.min_cost_multiple * rt:
                audits.append(AuditEvent("sizing", "cost_gate",
                                         f"{gid}: edge Rs{edge_rupees:.0f} < {tier.min_cost_multiple:.1f}x cost Rs{rt:.0f}"))
                return False
        return True

    def _enforce_caps(self, groups: dict[str, list[Component]],
                      kept: dict[str, list[tuple[Component, int]]], book: TargetBook,
                      tier: TierState, state: MarketState, positions: dict[str, Position],
                      sigma_daily: dict[str, float], ctx: RiskContext,
                      audits: list[AuditEvent], cut: set[str] | None) -> None:
        """Shrink `kept` in place until cap_breaches() is empty.

        The same monotone, group-joint rule as the exposure rules: every group
        touching the first breach is scaled by cap / current and re-rounded
        from its fractional quantities, so hedge ratios are kept as well as
        lot rounding allows. When rounding absorbs the whole shrink, the group
        adding most to the breach sheds one lot, so every pass removes at
        least one lot and the loop ends.
        """
        scale = dict.fromkeys(kept, 1.0)
        while True:
            breaches = cap_breaches(_net(kept), ctx)
            if not breaches:
                return
            b = breaches[0]
            touched = [g for g in sorted(kept)
                       if not b.symbols or any(c.symbol in b.symbols for c, _ in kept[g])]
            f = b.cap / b.current
            audits.append(AuditEvent(
                "risk", "target_cap",
                f"{b.rule} {b.key}: {b.current:,.0f} -> {b.cap:,.0f} on lot-rounded targets, "
                f"{len(touched)} group(s) x{f:.4f}", before=b.current, after=b.cap))
            lots_before = _lots(kept, state)
            for g in touched:
                scale[g] *= f
            if all(_qtys(self._round(groups[g], scale[g], state)) == _qtys(kept[g])
                   for g in touched):
                top, leg, leg_q = _largest_contributor(touched, kept, b, ctx)
                lot = max(state.instruments[leg.symbol].lot_size, 1)
                # half a lot below the current count: the floor lands one lot lower
                scale[top] = min(scale[top], (abs(leg_q) / lot - 0.5) * lot / abs(leg.qty))
            for g in touched:
                rounded = self._round(groups[g], scale[g], state)
                if _qtys(rounded) == _qtys(kept[g]):
                    continue
                if cut is not None:
                    cut.update(c.symbol for c, _ in kept[g])
                zero = next((c for c, q in rounded if q == 0), None)
                if zero is not None:
                    audits.append(AuditEvent("sizing", "rounds_to_zero", g, symbol=zero.symbol,
                                             before=zero.qty * scale[g], after=0.0))
                    del kept[g]
                elif self._tradeable(g, rounded, book.group_meta.get(g), tier, state, positions,
                                     sigma_daily, audits):
                    kept[g] = rounded
                else:
                    del kept[g]
            if _lots(kept, state) >= lots_before:
                raise RuntimeError(f"cap re-verification made no progress on {b}")

    @staticmethod
    def _drop_equity_shorts(kept: dict[str, list[tuple[Component, int]]], state: MarketState,
                            audits: list[AuditEvent], cut: set[str] | None) -> None:
        """Drop, whole, every group with a short leg on a cash equity whose
        net target is short, until no equity nets short. A short leg netted
        into a larger long is only a smaller long and stays. Dropping whole
        groups keeps pairs two-legged; refusing the short order on its own
        at the broker sent the long leg unhedged."""
        while True:
            net = _net(kept)
            short = sorted(s for s, q in net.items()
                           if q < 0 and state.instruments[s].kind == InstrumentKind.EQUITY)
            if not short:
                return
            for gid in sorted(kept):
                legs = [(c, q) for c, q in kept[gid] if c.symbol in short and q < 0]
                if legs:
                    audits.append(AuditEvent(
                        "sizing", "equity_short_blocked",
                        f"{gid}: short {', '.join(c.symbol for c, _ in legs)} cannot be "
                        "carried overnight in the cash segment", symbol=legs[0][0].symbol,
                        before=float(legs[0][1]), after=0.0))
                    if cut is not None:
                        cut.update(c.symbol for c, _ in kept[gid])
                    del kept[gid]

    def _promotion_candidate(self, comps: list[Component], state: MarketState) -> bool:
        """Index-futures unlock: one contract is the market's minimum ticket.
        Single-leg lot-sized FUTURE groups only; promoting one leg of a spread
        would corrupt the hedge ratio."""
        if not self.cfg.min_lot_promotion or len(comps) != 1:
            return False
        inst = state.instruments[comps[0].symbol]
        return inst.kind == InstrumentKind.FUTURE and inst.lot_size > 1

    def _promote(self, gid: str, c: Component, sig: Signal | None,
                 kept: dict[str, list[tuple[Component, int]]], tier: TierState,
                 state: MarketState, positions: dict[str, Position],
                 sigma_daily: dict[str, float], ctx: RiskContext | None, risk_scale: float,
                 audits: list[AuditEvent]) -> None:
        """Take exactly one lot iff it risks at most promotion_max_risk_frac of
        equity, scaled by the same throttle and regime scaler as the book, and
        every cap in `ctx` still holds with it. A promotion is a minimum ticket,
        never a way around the sizing: a zero qty or a qty on the wrong side of
        its signal is not promoted, and the lot takes the signal's side."""
        inst = state.instruments[c.symbol]
        side = _signal_side(sig, c.symbol)
        lot_risk = inst.lot_size * c.stop_distance * inst.point_value
        ceiling = self.cfg.promotion_max_risk_frac * state.equity * risk_scale
        reason: str | None = None
        rounded = [(c, side * inst.lot_size)] if side is not None else []
        if side is None:
            reason = "no signal side for the leg"
        elif c.qty == 0 or (c.qty > 0) != (side > 0):
            reason = f"qty {c.qty:+.2f} is not on the signal side"
        elif not (state.equity > 0 and lot_risk <= ceiling):
            reason = f"lot risk Rs{lot_risk:,.0f} > ceiling Rs{ceiling:,.0f}"
        elif ctx is None:
            reason = "no exposure caps to verify the lot against"
        elif not self._tradeable(gid, rounded, sig, tier, state, positions, sigma_daily, audits):
            reason = "the lot fails the notional floor or the cost gate"
        else:
            breach = next(iter(cap_breaches(_net({**kept, gid: rounded}), ctx)), None)
            if breach is not None:
                reason = (f"{breach.rule} {breach.key}: {breach.current:,.0f} > "
                          f"{breach.cap:,.0f} with the lot")
        if reason is not None:
            audits.append(AuditEvent("sizing", "min_lot_promotion_rejected", f"{gid}: {reason}",
                                     symbol=c.symbol, before=c.qty, after=0.0))
            audits.append(AuditEvent("sizing", "rounds_to_zero", gid, symbol=c.symbol,
                                     before=c.qty, after=0.0))
            return
        kept[gid] = rounded
        audits.append(AuditEvent("sizing", "min_lot_promotion", gid, symbol=c.symbol,
                                 before=c.qty, after=float(rounded[0][1])))

    @staticmethod
    def _is_new_trade(rounded: list[tuple[Component, int]], positions: dict[str, Position]) -> bool:
        """A group is 'held' iff every leg already has a same-direction position."""
        for c, q in rounded:
            pos = positions.get(c.symbol)
            if pos is None or pos.qty == 0 or (pos.qty > 0) != (q > 0):
                return True
        return False


def _qtys(rounded: list[tuple[Component, int]]) -> list[int]:
    return [q for _, q in rounded]


def _net(kept: Mapping[str, list[tuple[Component, int]]]) -> dict[str, float]:
    out: dict[str, float] = {}
    for rounded in kept.values():
        for c, q in rounded:
            out[c.symbol] = out.get(c.symbol, 0.0) + q
    return out


def _lots(kept: Mapping[str, list[tuple[Component, int]]], state: MarketState) -> int:
    return sum(abs(q) // max(state.instruments[c.symbol].lot_size, 1)
               for rounded in kept.values() for c, q in rounded)


def _largest_contributor(touched: list[str], kept: Mapping[str, list[tuple[Component, int]]],
                         b: CapBreach, ctx: RiskContext) -> tuple[str, Component, int]:
    """The touched group whose legs add most to breach `b`, and its leg that
    adds most. A leg adds to a netted measure when it has the sign of the net
    it sits in: its symbol's net, or the book's net for the net cap."""
    net = _net(kept)

    def notional(sym: str, q: float) -> float:
        pv = ctx.instruments[sym].point_value if sym in ctx.instruments else 1.0
        return q * ctx.prices.get(sym, 0.0) * pv

    book_net = sum(notional(s, q) for s, q in net.items())

    def weight(c: Component, q: int) -> float:
        ref = book_net if b.rule == "net_cap" else net[c.symbol]
        return notional(c.symbol, q) * math.copysign(1.0, ref)

    ranked: list[tuple[float, str, Component, int]] = []
    for g in touched:
        legs = [(c, q) for c, q in kept[g] if not b.symbols or c.symbol in b.symbols]
        c, q = max(legs, key=lambda cq: weight(*cq))
        ranked.append((sum(weight(*cq) for cq in legs), g, c, q))
    _, g, c, q = max(ranked, key=lambda r: (r[0], r[1]))
    return g, c, q


def _signal_side(sig: Signal | None, symbol: str) -> int | None:
    """+1 or -1: the side `sig` wants on `symbol` (direction sign times the
    leg's notional-ratio sign). None when there is no signal or no such leg."""
    if sig is None or sig.direction == 0:
        return None
    for leg in sig.resolved_legs():
        if leg.symbol == symbol and leg.notional_ratio != 0:
            return (1 if sig.direction > 0 else -1) * (1 if leg.notional_ratio > 0 else -1)
    return None
