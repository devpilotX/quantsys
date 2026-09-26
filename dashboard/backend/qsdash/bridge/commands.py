"""CommandConsumer — the engine-side half of the control plane.

Runs inside the engine process between decision bars. Commands transition
pending -> acked -> done|rejected and every outcome lands back in the row's
``result`` so the UI can show exactly what happened. The engine is the ONLY
thing that flips runtime_config['mode'] — the badge shows engine truth.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING

from qsdash.bridge.livebroker import LiveExecutionBroker
from qsdash.bus import SyncPublisher
from qsdash.db import SessionLocal, now_ist
from qsdash.models import Command, RuntimeConfig

if TYPE_CHECKING:  # pragma: no cover
    from qsdash.bridge.runner import Runner

log = logging.getLogger(__name__)


class CommandConsumer:
    def __init__(self, runner: Runner, publisher: SyncPublisher):
        self.runner = runner
        self.publisher = publisher

    def poll(self) -> int:
        """Apply every pending command. Returns how many were handled, so the
        runner knows when the engine state changed between bars."""
        handled = 0
        sess = SessionLocal()
        try:
            pending = (
                sess.query(Command)
                .filter(Command.status == "pending")
                .order_by(Command.id.asc())
                .all()
            )
            for cmd in pending:
                cmd.status = "acked"
                cmd.acked_at = now_ist()
                sess.commit()
                self._publish(cmd)
                try:
                    result = self._apply(cmd, sess)
                    cmd.status = "done"
                    cmd.result = result or {"ok": True}
                except _Reject as r:
                    cmd.status = "rejected"
                    cmd.result = {"ok": False, "reason": str(r)}
                    log.warning("command %s rejected: %s", cmd.kind, r)
                except Exception as e:
                    cmd.status = "rejected"
                    cmd.result = {"ok": False, "reason": f"error: {e}"}
                    log.exception("command %s failed", cmd.kind)
                cmd.completed_at = now_ist()
                sess.commit()
                self._publish(cmd)
                handled += 1
        finally:
            sess.close()
        return handled

    def _publish(self, cmd: Command) -> None:
        self.publisher.publish("commands", {
            "id": cmd.id, "kind": cmd.kind, "status": cmd.status,
            "result": cmd.result,
        })

    def _set_rc(self, sess, key: str, value) -> None:
        row = sess.query(RuntimeConfig).filter(RuntimeConfig.key == key).first()
        if row is None:
            row = RuntimeConfig(key=key, value={"v": value}, updated_by="engine")
            sess.add(row)
        else:
            row.value = {"v": value}
            row.version += 1
            row.updated_at = now_ist()
            row.updated_by = "engine"

    # ---------------------------------------------------------------- apply
    def _apply(self, cmd: Command, sess) -> dict:
        r = self.runner
        kind = cmd.kind
        p = cmd.payload or {}

        if kind == "set_mode":
            target = p.get("target_mode")
            if target not in ("paper", "live"):
                raise _Reject(f"bad target mode {target!r}")
            if target == r.mode:
                raise _Reject(f"already in {target}")
            if target == "live":
                # Engine-side gate (defence in depth on top of the dashboard's
                # operator chain): adapter health + out-of-band arm + a passed
                # real-data backtest. Refuses on synthetic-only history.
                from qsdash.bridge.livegate import live_gate

                gate = live_gate(
                    sess,
                    adapter_present=getattr(r, "supports_live", False),
                    adapter_connected=getattr(r, "adapter_connected", False),
                )
                if not gate.allowed:
                    raise _Reject("live gate refused: " + "; ".join(gate.reasons))
            # transition: flatten the OUTGOING book first if requested
            if p.get("flatten_first") and r.broker is not None:
                summary = r.broker.flatten_all(r.current_prices(), now_ist(),
                                               reason="mode switch flatten")
                blocked = (summary or {}).get("blocked") or {}
                if blocked:
                    raise _Reject("mode switch refused, flatten incomplete: " + "; ".join(
                        f"{sym} ({why})" for sym, why in sorted(blocked.items())))
            r.mode = target
            self._set_rc(sess, "mode", target)
            # The request's caps take effect only now the switch is made: the
            # runtime_config cap keys are shared with the paper engine, and a
            # refused go-live must not leave a live cap behind in them.
            caps = {k: float(p[k]) for k in ("deployable_cap_frac", "deployable_cap_abs")
                    if p.get(k) is not None}
            if caps:
                before = self._engine_equity()
                for key, value in caps.items():
                    setattr(r, key, value)
                    self._set_rc(sess, key, value)
                self._rebase(before)
            return {"ok": True, "mode": target, **caps,
                    "gate_run_id": getattr(gate, "passing_run_id", None)
                    if target == "live" else None}

        if kind == "set_paper_capital":
            cap = float(p.get("capital", 0))
            if not (math.isfinite(cap) and cap > 0):
                raise _Reject("capital must be positive")
            if r.broker is not None and r.broker.positions:
                raise _Reject("close all paper positions before changing capital")
            before = self._engine_equity()
            r.reset_paper_capital(cap)
            self._rebase(before)
            # both keys so the startup path sees value == applied and does NOT
            # re-reset cash on the next recycle (phantom-equity guard)
            self._set_rc(sess, "paper_capital", cap)
            self._set_rc(sess, "paper_capital_applied", cap)
            return {"ok": True, "capital": cap}

        if kind == "set_deployable_cap":
            before = self._engine_equity()
            if "deployable_cap_frac" in p:
                r.deployable_cap_frac = float(p["deployable_cap_frac"])
            if "deployable_cap_abs" in p:
                r.deployable_cap_abs = float(p["deployable_cap_abs"])
            self._rebase(before)
            return {"ok": True,
                    "deployable_cap_frac": r.deployable_cap_frac,
                    "deployable_cap_abs": r.deployable_cap_abs}

        if kind == "kill":
            r.engine.risk.dd_killed = True
            return {"ok": True, "note": "kill armed — engine will flatten on next bar"}

        if kind == "flatten":
            if r.broker is None:
                raise _Reject("no broker attached")
            # the live broker tags these orders as a flatten, so they never
            # take the id of a decision order from the same minute
            summary = r.broker.flatten_all(r.current_prices(), now_ist(),
                                           reason=p.get("reason") or "operator flatten")
            blocked = (summary or {}).get("blocked") or {}
            if blocked:
                # a symbol left open must not read as a finished flatten
                raise _Reject("flatten incomplete, still open: " + "; ".join(
                    f"{sym} ({why})" for sym, why in sorted(blocked.items())))
            return {"ok": True, **(summary or {})}

        if kind == "rearm_dd_kill":
            # clears the kills and the halt, and measures drawdown from the
            # next equity: re-arming without the re-base killed again next bar
            r.engine.risk.rearm()
            return {"ok": True}

        if kind == "clear_halt":
            # only the reconciliation freeze; the drawdown reference and any
            # kill stay as they are
            r.engine.risk.clear_halt()
            if isinstance(r.broker, LiveExecutionBroker):
                # the intended book has not changed, so neither has the mismatch
                return {"ok": True, "note": (
                    "reconciliation runs again on the next bar, and a mismatch that "
                    "persists freezes the engine again; once the difference is "
                    "explained, rebaseline_live_book takes the broker's book as the "
                    "new baseline")}
            return {"ok": True}

        if kind == "rebaseline_live_book":
            # The remedy for a reconciliation freeze whose cause is understood
            # (a lost postback, a trade made at the broker): the broker's book
            # becomes the intended book's new baseline, then the halt clears.
            if not isinstance(r.broker, LiveExecutionBroker):
                raise _Reject("rebaseline_live_book needs the live execution broker; "
                              f"this engine runs {r.mode}")
            out: dict = {"ok": True, "baseline": r.broker.rebaseline()}
            r.engine.risk.clear_halt()
            working = sorted(r.broker.working_symbols())
            if working:
                out["working"] = working
                out["note"] = ("fills on these symbols that are at the broker but not yet "
                               "booked by a postback are now counted twice; if "
                               "reconciliation freezes again, re-baseline once they finish")
            return out

        if kind == "strategy_toggle":
            name = p.get("strategy", "")
            enabled = bool(p.get("enabled", True))
            r.set_strategy_enabled(name, enabled)
            return {"ok": True, "strategy": name, "enabled": enabled}

        if kind == "set_config":
            key, value = p.get("key", ""), p.get("value")
            r.apply_config_override(key, value)
            return {"ok": True, "key": key, "value": value}

        if kind in ("engine_pause", "engine_resume"):
            # Pause HALTS the decision loop without flattening (distinct from
            # kill, which flattens). Persisted so a pause survives an engine
            # restart — you must explicitly resume. Bars keep recording; the
            # engine just stops deciding/executing.
            paused = kind == "engine_pause"
            r.paused = paused
            self._set_rc(sess, "engine_paused", paused)
            return {"ok": True, "paused": paused,
                    "note": "decision loop halted (no flatten)" if paused
                    else "decision loop resumed"}

        raise _Reject(f"unknown command kind {kind!r}")

    # ------------------------------------------------------ capital changes
    def _engine_equity(self) -> float | None:
        """Equity as the engine sees it (after the deployable caps), or None
        when it cannot be read."""
        r = self.runner
        if r.broker is None:
            return None
        try:
            return float(r.effective_equity(r.broker.equity(r.current_prices())))
        except Exception as e:  # a broker read failure must not block the command
            log.warning("equity unreadable around a capital change: %s", e)
            return None

    def _rebase(self, before: float | None) -> None:
        """An operator capital change is neither a gain nor a loss: scale the
        drawdown and day-loss references by new / old. Without this, cutting
        deployable capital by a fifth read as a 20% drawdown and flattened
        the book."""
        after = self._engine_equity()
        if (before is None or after is None or not (math.isfinite(before) and before > 0)
                or not (math.isfinite(after) and after > 0)):
            log.warning("capital change: equity before=%r after=%r, references not "
                        "re-based", before, after)
            return
        if after != before:
            self.runner.engine.risk.rebase_equity(before, after)
            log.info("capital change: drawdown references scaled by %.6f", after / before)


class _Reject(Exception):
    pass
