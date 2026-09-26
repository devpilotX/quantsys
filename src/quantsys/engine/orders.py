"""Target-vs-position order diffing with anti-churn bands.

Rules:
- Full exits (target 0) and risk-driven orders always go out.
- Rebalance deltas smaller than max(1 lot, band * |target|) are skipped —
  at small capital the band is wide (cost drag), at large capital narrow.
- Sub-min-notional dribbles are skipped unless exiting.
- A reduction of a symbol in cap_reducing_symbols (held above a cap, or cut
  by the exposure rules or the drawdown throttle this bar) skips both filters
  and needs only one lot: the band exists to save costs, not to keep a
  position the risk layer has just cut.
- The legs of a multi-leg group move together: symbols linked through a
  group's legs are sent all together or not at all, so no filter can break a
  hedge ratio or open one leg alone.
- KILL flattens at market; stop hits are RISK_REDUCING (cross the spread);
  everything else uses the tier's execution style.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping

from quantsys.core.types import (
    ExecutionStyle,
    Instrument,
    OrderIntent,
    Position,
    TargetPosition,
    Urgency,
)
from quantsys.portfolio.tiers import TierState


def diff_orders(
    targets: list[TargetPosition],
    positions: Mapping[str, Position],
    instruments: Mapping[str, Instrument],
    prices: Mapping[str, float],
    tier: TierState,
    min_order_notional: float,
    risk_reducing_symbols: Collection[str] = frozenset(),
    kill: bool = False,
    cap_reducing_symbols: Collection[str] = frozenset(),
) -> list[OrderIntent]:
    net: dict[str, int] = {}
    strat: dict[str, dict[str, int]] = {}
    for t in targets:
        net[t.symbol] = net.get(t.symbol, 0) + t.qty
        strat.setdefault(t.symbol, {})[t.strategy] = (
            strat.get(t.symbol, {}).get(t.strategy, 0) + abs(t.qty)
        )
    linked = _linked_symbols(targets)

    candidates: dict[str, OrderIntent] = {}
    passes: dict[str, bool] = {}
    out: list[OrderIntent] = []
    for sym in sorted(set(net) | {s for s, p in positions.items() if p.qty != 0}):
        cur = positions[sym].qty if sym in positions else 0
        tgt = net.get(sym, 0)

        if kill:  # flatten unconditionally — before any delta short-circuit
            if cur != 0:
                out.append(OrderIntent(sym, -cur, ExecutionStyle.MARKET_SINGLE,
                                       Urgency.KILL, reason="kill_switch"))
            continue

        delta = tgt - cur
        if delta == 0:
            continue
        inst = instruments.get(sym)
        lot = max(inst.lot_size, 1) if inst else 1
        price = prices.get(sym, 0.0)

        urgency = Urgency.RISK_REDUCING if sym in risk_reducing_symbols else Urgency.NORMAL
        ok = True
        if tgt != 0 and urgency is Urgency.NORMAL:
            reducing = (tgt > 0) == (cur > 0) and abs(tgt) < abs(cur)
            if reducing and sym in cap_reducing_symbols:
                ok = abs(delta) >= lot  # past the band, not past the dust floor
            elif abs(delta) < max(lot, tier.rebalance_band * abs(tgt)):
                ok = False  # anti-churn band
            # The dust floor binds every partial order, cap-driven cuts included:
            # waiving it re-opened the one-share dribbles the equity-scaled
            # floor was added to stop (Forward Study 2 day-1 defect 2).
            if ok and price > 0 and abs(delta) * price * (inst.point_value if inst else 1.0) < min_order_notional:
                ok = False

        dominant = max(strat.get(sym, {"": 0}), key=lambda k: strat.get(sym, {}).get(k, 0))
        style = ExecutionStyle.MARKET_SINGLE if urgency is not Urgency.NORMAL else tier.execution_style
        candidates[sym] = OrderIntent(sym, delta, style, urgency, strategy=dominant,
                                      reason="rebalance" if cur and tgt else ("exit" if tgt == 0 else "entry"))
        passes[sym] = ok
    if kill:
        return out

    for sym in sorted(candidates):
        group = linked.get(sym, (sym,))
        if any(passes.get(s, False) for s in group):
            out.append(candidates[sym])
    return out


def _linked_symbols(targets: list[TargetPosition]) -> dict[str, tuple[str, ...]]:
    """symbol -> every symbol joined to it through the legs of multi-leg
    groups (transitively). Symbols only in single-leg groups are absent."""
    legs: dict[str, set[str]] = {}
    for t in targets:
        legs.setdefault(t.group_id, set()).add(t.symbol)
    parent: dict[str, str] = {}

    def root(s: str) -> str:
        while parent[s] != s:
            s = parent[s]
        return s

    for symbols in legs.values():
        if len(symbols) < 2:
            continue
        first, *rest = sorted(symbols)
        parent.setdefault(first, first)
        for s in rest:
            parent.setdefault(s, s)
            parent[root(s)] = root(first)
    members: dict[str, list[str]] = {}
    for s in parent:
        members.setdefault(root(s), []).append(s)
    return {s: tuple(sorted(members[root(s)])) for s in parent}
