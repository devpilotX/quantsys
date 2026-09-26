"""Position reconciliation: broker is ground truth.

Compares the engine's intended/known book against the broker's reported
positions. Any mismatch beyond a lot tolerance is fed into
RiskEngine.reconcile(), which FREEZES the engine (no orders) — flattening on
top of an untrusted book could double the error. This mirrors the core's
kill-vs-halt semantics exactly.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field

from quantsys.execution.broker import Broker, BrokerError

log = logging.getLogger("quantsys.reconcile")


@dataclass
class ReconResult:
    ok: bool
    internal: dict[str, int]
    broker: dict[str, int]
    mismatches: list[str]
    skipped: list[str] = field(default_factory=list)


def reconcile_books(internal: Mapping[str, int], broker: Mapping[str, int],
                    skip: Collection[str] = ()) -> ReconResult:
    """Compare an engine-side book with one broker snapshot. Symbols in
    ``skip`` (orders still working, so fills may be in flight) are left out
    of both returned maps, so RiskEngine.reconcile() given those maps sees
    exactly the mismatches reported here."""
    skipped = sorted({s for s in set(internal) | set(broker) if s in skip})
    ours = {s: q for s, q in internal.items() if q != 0 and s not in skip}
    theirs = {s: q for s, q in broker.items() if q != 0 and s not in skip}
    mismatches = [f"{sym}: internal={ours.get(sym, 0)} broker={theirs.get(sym, 0)}"
                  for sym in sorted(set(ours) | set(theirs))
                  if ours.get(sym, 0) != theirs.get(sym, 0)]
    return ReconResult(not mismatches, ours, theirs, mismatches, skipped)


def reconcile_positions(broker: Broker, internal: dict[str, int]
                        ) -> ReconResult:
    """Returns the maps the RiskEngine.reconcile() expects + a human summary.
    On a broker read failure we report NOT-ok with an explicit cause so the
    caller freezes rather than trusting a stale internal view."""
    try:
        bpos = {p.symbol: p.qty for p in broker.positions() if p.qty != 0}
    except BrokerError as e:
        return ReconResult(False, internal, {}, [f"broker positions unreadable: {e}"])
    return reconcile_books(internal, bpos)
