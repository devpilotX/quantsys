"""LiveRunner — the 24x7 live/paper-on-live-data orchestrator.

Wires: AngelOneBroker (data + execution) -> BarAggregator -> DecisionEngine ->
LiveExecutionBroker (OMS) -> Recorder, with reconciliation every cycle and
market-hours gating. Shares the Recorder, engine, and CommandConsumer with the
paper runner; the difference is the venue and that real ticks drive the clock.

Modes:
- mode='paper'  : real live data feed, but LiveExecutionBroker is NOT used —
                  a PaperBroker fills locally. This is "paper-on-VPS", the
                  first test of the feed/postback/reconcile plumbing.
- mode='live'   : real orders, gated by livegate (adapter + QS_LIVE_ARMED +
                  passed real backtest). Refused otherwise.

Run:  QS_LIVE_ARMED=0 python -m qsdash.bridge.live --paper      # paper on live data
      QS_LIVE_ARMED=1 python -m qsdash.bridge.live --live       # real money (gated)

Real-money trading requires rotated credentials, the VPS whitelisted IP, and a
passed real-data backtest — see docs/GOLIVE.md. This module is complete and
tested with a mock transport; it has NOT been run against the live broker.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
import math
import threading
import time
from datetime import date, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError

from qsdash.audit import notify_alert
from qsdash.bridge.commands import CommandConsumer
from qsdash.bridge.livebroker import LiveExecutionBroker
from qsdash.bridge.paper import PaperBroker
from qsdash.bridge.recorder import Recorder
from qsdash.bus import make_sync_publisher
from qsdash.db import SessionLocal, now_ist
from qsdash.models import EngineState, MarketBar, RuntimeConfig
from quantsys.config import load_config
from quantsys.core.market_state import MarketState
from quantsys.core.types import Bar, InstrumentKind, Position, bars_per_day, is_session_open
from quantsys.data.history import BarHistory
from quantsys.engine.decision import DecisionEngine
from quantsys.execution.angelone import AngelOneBroker
from quantsys.execution.broker import BrokerError
from quantsys.execution.marketdata import BarAggregator

log = logging.getLogger("qsdash.live")

FEED_STALE_S = 90  # no ticks for this long during market hours => alert

# decision-bar minutes -> Angel candle interval, for startup history warmup
_ANGEL_INTERVAL = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE",
                   10: "TEN_MINUTE", 15: "FIFTEEN_MINUTE", 30: "THIRTY_MINUTE",
                   60: "ONE_HOUR"}

# Warm-up history in decision bars: at least this many, more when a sleeve's
# lookback or the regime model's training window needs it.
WARMUP_MIN_BARS = 2000
# Of those, the newest this many run through decide(); older bars only fill
# the histories. Deciding every bar of a 4,700-bar window took ~5 minutes.
WARMUP_DECIDE_BARS = 2000
# Calendar days per session, with room for weekends and exchange holidays.
_CALENDAR_PER_SESSION = 1.6

ENGINE_STATE_VERSION = 1


def _json_safe(obj, path: str, bad: list[str]):
    """``obj`` as plain JSON: numpy scalars unwrapped, tuples as lists, and a
    non-finite float replaced by None and its path recorded (Postgres JSONB
    refuses NaN, so one would fail every save)."""
    if isinstance(obj, dict):
        return {str(k): _json_safe(v, f"{path}.{k}", bad) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v, f"{path}[{i}]", bad) for i, v in enumerate(obj)]
    item = getattr(obj, "item", None)
    if callable(item) and not isinstance(obj, (str, bytes)):
        obj = item()
    if isinstance(obj, float) and not math.isfinite(obj):
        bad.append(path)
        return None
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    raise TypeError(f"engine state at {path} is not JSON-serialisable: {type(obj).__name__}")


class LiveRunner:
    # False once a saved engine state failed to restore (see _restore_engine_state)
    _state_save_enabled = True
    # raw equity before a paper-capital reset applied at start-up, if any
    _startup_raw_equity: float | None = None
    _state_key: str | None = None

    def __init__(self, cfg_path: str, mode: str = "paper",
                 broker: AngelOneBroker | None = None,
                 paper_capital: float = 1_000_000.0):
        self.cfg = load_config(cfg_path)
        self.cfg_path = cfg_path
        self.mode = mode
        # The saved engine state belongs to this process's role. mode can be
        # switched at run time; the row it saves to must not follow it.
        self._state_key = mode
        self._apply_paper_exploration()
        self._apply_live_constraints()
        self.publisher = make_sync_publisher(SessionLocal)

        self.broker_adapter = broker or AngelOneBroker()
        self.broker_adapter.connect()
        # instrument master is the live source of truth for lot/tick/token;
        # config values are only warm-start fallbacks (merge: master wins,
        # fall back to config for fields the master lacks like point_value/adv)
        self.instruments = self._merge_instruments(self.broker_adapter.instruments())
        self.engine = DecisionEngine(self.cfg, instruments=self.instruments)
        self._all_strategies = list(self.engine.strategies)
        self._disabled: set[str] = set()

        self.recorder = Recorder(mode, self.publisher)
        if mode == "live":
            self.broker = LiveExecutionBroker(self.broker_adapter,
                                              self.instruments, self.publisher)
        else:
            self.broker = PaperBroker(mode, paper_capital, self.instruments,
                                      self.engine.cost_model, self.publisher)
            self.broker.load_open_state()
        self.commands = CommandConsumer(self, self.publisher)

        self.histories: dict[str, BarHistory] = {s: BarHistory() for s in self.instruments}
        self._last_prices: dict[str, float] = {}
        self.deployable_cap_frac: float | None = None
        self.deployable_cap_abs: float | None = None
        self._pending_bars: dict[str, Bar] = {}
        self._bar_accum: dict[str, Bar] = {}      # completed bars awaiting the bucket decide
        self._last_step_bucket: datetime | None = None
        self._lock = threading.Lock()
        self.feed = None  # set by main() to the AngelWebSocketFeed (feed health)
        # alert transition state (only alert on state CHANGES, not every bar)
        self._last_kill: str | None = None
        self._last_halted = False
        self._feed_stale = False
        self._started_monotonic = 0.0
        self.aggregator = BarAggregator(self.cfg.engine.decision_bar_minutes,
                                        self._on_completed_bar)
        self.paused = False              # operator pause: halt decisions, no flatten
        self._publish_config_snapshot()
        self._load_runtime_config()

    # ----------------------------------------------------- gate properties
    @property
    def supports_live(self) -> bool:
        return isinstance(self.broker, LiveExecutionBroker)

    @property
    def adapter_connected(self) -> bool:
        try:
            return self.broker_adapter.is_connected()
        except Exception:
            return False

    # ------------------------------------------------------------- config
    def _apply_paper_exploration(self) -> None:
        """Paper-ONLY: force a small edge-agnostic Kelly allocation and bypass
        the cost gate so the engine exercises the full order -> fill ->
        reconcile -> P&L path even when no strategy has positive edge after
        costs. Mutates the in-memory config BEFORE the engine is built. Never
        runs in live (mode != 'paper' -> the honest gate stays intact) and never
        in the backtest (which builds the engine directly, not via LiveRunner).
        Toggle with engine.paper_explore in config."""
        if self.mode != "paper" or not self.cfg.engine.paper_explore:
            return
        self.cfg.kelly.explore_floor = max(self.cfg.kelly.explore_floor,
                                           self.cfg.engine.paper_explore_floor)
        if self.cfg.engine.paper_explore_bypass_cost_gate:
            self.cfg.sizing.enforce_cost_gate = False
        gate = "bypassed" if not self.cfg.sizing.enforce_cost_gate else "ENFORCED"
        log.warning("PAPER EXPLORATION ON: kelly.explore_floor=%.3f, cost gate "
                    "%s — paper-validation trades only (live/backtest are "
                    "unaffected and stay honest)", self.cfg.kelly.explore_floor, gate)

    def _apply_live_constraints(self) -> None:
        """Live-ONLY: a cash-segment equity short cannot be carried overnight
        in India, so the sizer drops any group with one (whole, so a pair
        never goes out one-legged). The live broker refusing the sell on its
        own, order by order, sent the other leg of the pair unhedged."""
        if self.mode != "live":
            return
        if self.cfg.sizing.allow_equity_shorts:
            log.warning("LIVE: equity shorts disabled in sizing (cash-segment shorts "
                        "cannot be carried overnight); groups with one are dropped whole")
        self.cfg.sizing.allow_equity_shorts = False

    def _merge_instruments(self, master: dict) -> dict:
        from quantsys.core.types import Instrument
        out = {}
        cfg_by_sym = {u.symbol: u for u in self.cfg.universe}
        for sym, u in cfg_by_sym.items():
            m = master.get(sym)
            if m is not None:
                out[sym] = Instrument(
                    symbol=sym, token=m.token or u.token, exchange=m.exchange,
                    kind=m.kind, lot_size=m.lot_size, tick_size=m.tick_size,
                    point_value=u.point_value, sector=u.sector, adv=u.adv,
                    margin_rate=u.margin_rate,
                )
            else:
                log.warning("instrument %s not in master — using config fallback", sym)
                out[sym] = Instrument(
                    symbol=u.symbol, token=u.token, exchange=u.exchange, kind=u.kind,
                    lot_size=u.lot_size, tick_size=u.tick_size, point_value=u.point_value,
                    sector=u.sector, adv=u.adv, margin_rate=u.margin_rate,
                )
        return out

    def _publish_config_snapshot(self) -> None:
        sess = SessionLocal()
        try:
            ladder = [{"name": t.name, "min_equity": t.min_equity,
                       "max_instruments": t.max_instruments,
                       "max_strategies": t.max_strategies}
                      for t in self.cfg.tiers.ladder]
            for key, val in (("engine.tier_ladder", ladder),
                             ("engine.tier_hysteresis", self.cfg.tiers.hysteresis),
                             ("mode", self.mode)):
                row = sess.query(RuntimeConfig).filter(RuntimeConfig.key == key).first()
                if row is None:
                    sess.add(RuntimeConfig(key=key, value={"v": val}, updated_by="engine"))
                else:
                    row.value = {"v": val}
                    row.updated_at = now_ist()
            sess.commit()
        finally:
            sess.close()

    def _load_runtime_config(self) -> None:
        sess = SessionLocal()
        try:
            rc = {r.key: r.value.get("v") for r in sess.query(RuntimeConfig).all()}
        finally:
            sess.close()
        self.deployable_cap_frac = rc.get("deployable_cap_frac")
        self.deployable_cap_abs = rc.get("deployable_cap_abs")
        self.paused = bool(rc.get("engine_paused", False))  # durable across restart
        # Durable paper capital: applied ONLY when the operator value CHANGED
        # (tracked via the paper_capital_applied marker). Re-applying it on
        # every restart silently reset cash to the full float on top of a
        # carried book — a phantom equity jump equal to the open positions'
        # cost basis at every 08:50 recycle, compounding into the study's
        # equity series. Unchanged value => the broker's persisted cash /
        # realized (see PaperBroker.load_open_state) carries the true series.
        pc = rc.get("paper_capital")
        applied = rc.get("paper_capital_applied")
        if pc is not None and pc != applied:
            if isinstance(self.broker, PaperBroker) and self.broker.positions:
                log.error(
                    "durable paper_capital=%s NOT applied: open positions were "
                    "carried across the restart. Flatten first, then re-set — "
                    "keeping persisted cash so the equity series stays honest.", pc)
            else:
                try:
                    # flat book: equity is the cash, before and after
                    self._startup_raw_equity = float(self.broker.cash)
                    self.reset_paper_capital(float(pc))
                    self._set_runtime_key("paper_capital_applied", pc)
                    log.info("durable paper_capital applied (operator change): "
                             "Rs%.0f", float(pc))
                except (TypeError, ValueError):
                    log.warning("ignoring invalid durable paper_capital=%r", pc)
        for name in [s.name for s in self._all_strategies]:
            if rc.get(f"strategy_enabled.{name}") is False:
                self.set_strategy_enabled(name, False)

    def _set_runtime_key(self, key: str, value) -> None:
        sess = SessionLocal()
        try:
            row = sess.query(RuntimeConfig).filter(RuntimeConfig.key == key).first()
            if row is None:
                sess.add(RuntimeConfig(key=key, value={"v": value},
                                       updated_by="engine"))
            else:
                row.value = {"v": value}
                row.updated_at = now_ist()
                row.updated_by = "engine"
            sess.commit()
        finally:
            sess.close()

    # ---------------------------------------------------- control surface
    def current_prices(self) -> dict[str, float]:
        return dict(self._last_prices)

    def reset_paper_capital(self, capital: float) -> None:
        if isinstance(self.broker, PaperBroker):
            self.broker.cash = capital
            self.broker.realized_total = 0.0
            self.broker.persist_cash()

    def set_strategy_enabled(self, name: str, enabled: bool) -> None:
        if enabled:
            self._disabled.discard(name)
        else:
            self._disabled.add(name)
        self.engine.strategies = [s for s in self._all_strategies
                                  if s.name not in self._disabled]

    def apply_config_override(self, key: str, value) -> None:
        parts = key.split(".")
        obj = self.cfg
        for p in parts[:-1]:
            obj = getattr(obj, p)
        setattr(obj, parts[-1], type(getattr(obj, parts[-1]))(value))
        state = self.engine.state_dict()
        self.engine = DecisionEngine(self.cfg, instruments=self.instruments)
        self._all_strategies = list(self.engine.strategies)
        self.engine.load_state(state)
        self.engine.strategies = [s for s in self._all_strategies
                                  if s.name not in self._disabled]

    def effective_equity(self, raw_equity: float) -> float:
        e = raw_equity
        if self.deployable_cap_frac is not None:
            e = min(e, raw_equity * self.deployable_cap_frac)
        if self.deployable_cap_abs is not None:
            e = min(e, self.deployable_cap_abs)
        return e

    # ------------------------------------------------------- bar handling
    def _on_completed_bar(self, symbol: str, bar: Bar) -> None:
        """Called by the aggregator thread when a symbol's bar completes."""
        with self._lock:
            self._pending_bars[symbol] = bar
            self._last_prices[symbol] = bar.close

    def _drain_pending(self) -> dict[str, Bar]:
        with self._lock:
            batch = self._pending_bars
            self._pending_bars = {}
            return batch

    def step(self, ts: datetime, batch: dict[str, Bar]) -> None:
        """One decision cycle. Reconcile FIRST (freeze on mismatch), then
        decide, execute, record."""
        for sym, bar in batch.items():
            h = self.histories.get(sym)
            if h is not None:
                h.append(bar)
        self.recorder.record_bars(self.cfg.engine.decision_bar_minutes, batch)

        if self.paused:
            # operator pause: bars recorded (strategies stay warm), decision loop
            # halted — no reconcile/decide/execute, NO flatten (distinct from kill).
            self.recorder.heartbeat(
                status="paused", market_open=is_session_open(ts),
                detail={"data_source": "angelone", "venue": self.mode,
                        "bar_ts": ts.isoformat(),
                        "note": "operator pause: decisions halted, not flattened"})
            self._poll_commands(ts)
            return

        prices = self.current_prices()

        # Live: ONE broker read per bar, and the reconciliation, the risk
        # engine and the decision all see it. Reconciliation compares it with
        # the engine's own book (baseline + recorded fills); a mismatch freezes
        # (no orders). An unreadable broker halts the bar: no decide and no
        # orders, rather than trading on a fabricated flat book.
        if isinstance(self.broker, LiveExecutionBroker):
            try:
                snap = self.broker.refresh_snapshot()
            except BrokerError as e:
                log.error("bar %s halted: broker read failed: %s", ts, e)
                self.broker.record_risk_event(
                    "broker_unreadable", f"bar {ts.isoformat()} halted: {e}",
                    severity="crit", ts=ts)
                self.recorder.heartbeat(
                    status="halted", market_open=is_session_open(ts),
                    detail={"data_source": "angelone", "venue": self.mode,
                            "bar_ts": ts.isoformat(),
                            "halt": f"broker read failed: {e}"})
                self._poll_commands(ts)
                return
            recon = self.broker.reconcile(snap)
            if not recon.ok:
                self.engine.risk.reconcile(recon.internal, recon.broker)
                self._persist_engine_state(ts)  # the freeze must survive a restart
            positions = {s: Position(s, p.qty, p.avg_price)
                         for s, p in snap.positions.items()}
        else:
            positions = {s: Position(s, p.qty, getattr(p, "avg_price", 0.0))
                         for s, p in self.broker.positions.items()}

        foreign = sorted(s for s, p in positions.items() if p.qty and s not in self.instruments)
        if foreign:
            # Held at the broker but outside the universe: the engine cannot
            # price, trade or reconcile them, and they are not in its equity.
            # Flag them; halting on them would stop every decision, the kill
            # switch included.
            self._alert_foreign_holdings(foreign, ts)
            positions = {s: p for s, p in positions.items() if s in self.instruments}
        unmarked = sorted(s for s, p in positions.items() if p.qty and s not in prices)
        if unmarked:
            # A held position without a price is valued at nothing, which reads
            # as a loss of its whole notional and can fire the kill switch.
            # Skip the bar instead: no decision, no orders, and say why.
            log.error("bar %s halted: no price for held %s", ts, unmarked)
            self.recorder.heartbeat(
                status="halted", market_open=is_session_open(ts),
                detail={"data_source": "angelone", "venue": self.mode,
                        "bar_ts": ts.isoformat(), "halt": f"no price for held {unmarked}"})
            self._poll_commands(ts)
            return

        equity = self.broker.equity(prices)
        state = MarketState(
            ts=ts, equity=self.effective_equity(equity), bars=self.histories,
            instruments=self.instruments, positions=positions,
        )
        self.engine.post_bar(state)
        decision = self.engine.decide(state)
        decision_id = self.recorder.record_decision(decision, self.engine)
        self._alert_on_decision(decision)
        try:
            if isinstance(self.broker, PaperBroker):
                # a paper fill needs a print in this bucket, as in the backtest
                self.broker.execute(decision, decision_id, prices, ts, printed=set(batch))
            else:
                self.broker.execute(decision, decision_id, prices, ts)
        except BrokerError as e:
            log.error("execute failed (fail-safe: no new risk this bar): %s", e)

        prices = self.current_prices()
        gross, net = self.broker.exposures(prices)
        self.recorder.record_equity(
            ts, equity=self.broker.equity(prices), cash=self.broker.cash,
            mtm=self.broker.mtm(prices), gross=gross, net=net,
            realized=self.broker.realized_total,
            unrealized=self.broker.unrealized(prices),
        )
        status = ("killed" if decision.kill_reason else
                  "halted" if decision.halted else "running")
        self.recorder.heartbeat(
            status=status, market_open=is_session_open(ts),
            detail={"data_source": "angelone", "venue": self.mode,
                    "bar_ts": ts.isoformat(), "n_orders": len(decision.orders)},
        )
        self.commands.poll()
        self._persist_engine_state(ts)

    def _alert_foreign_holdings(self, symbols: list[str], ts: datetime) -> None:
        """One critical alert per holding outside the universe, per process."""
        seen = self.__dict__.setdefault("_foreign_alerted", set())
        new = [s for s in symbols if s not in seen]
        if not new:
            return
        seen.update(new)
        log.error("broker holds %s, outside the engine universe: not priced, traded "
                  "or counted in equity; close or move them at the broker", new)
        notify_alert(self.publisher, severity="crit", kind="foreign_holding",
                     title="Holding outside the engine universe",
                     body=f"{self.mode} at {ts.isoformat()}: {', '.join(new)}")

    def _poll_commands(self, bar_ts: datetime | None) -> None:
        """Apply pending operator commands; save the engine state if any ran,
        since a re-arm, a kill or a config change must survive a restart."""
        if self.commands.poll():
            self._persist_engine_state(bar_ts)

    # ------------------------------------------------------- engine state
    def _config_hash(self) -> str:
        blob = json.dumps(self.cfg.model_dump(mode="json"), sort_keys=True, default=str)
        return hashlib.sha256(blob.encode()).hexdigest()[:16]

    @property
    def _engine_state_key(self) -> str:
        return self._state_key or self.mode

    def _capital_basis(self) -> dict:
        return {"deployable_cap_frac": self.deployable_cap_frac,
                "deployable_cap_abs": self.deployable_cap_abs}

    def _effective_under(self, raw: float, basis: dict) -> float:
        """effective_equity(raw) under the deployable caps in ``basis``."""
        e = raw
        frac, cap = basis.get("deployable_cap_frac"), basis.get("deployable_cap_abs")
        if frac is not None:
            e = min(e, raw * float(frac))
        if cap is not None:
            e = min(e, float(cap))
        return e

    def _rebase_to_current_capital(self, saved_basis: dict) -> None:
        """The restored drawdown references are in the effective equity of
        the saved run. A deployable-cap change, or a paper-capital reset,
        applied from runtime_config at this start would read as a gain or a
        loss (a 20% cap cut latched the hard kill on the first bar). Scale
        the references by new / old effective equity of the same book. Only
        the capital settings move the ratio: market moves while the engine
        was down stay real P&L."""
        try:
            raw_now = float(self.broker.equity(self.current_prices()))
        except BrokerError as e:
            log.error("equity unreadable at start (%s): drawdown references not "
                      "re-based to the capital settings", e)
            return
        raw_before = self._startup_raw_equity if self._startup_raw_equity is not None else raw_now
        old = self._effective_under(raw_before, saved_basis)
        new = self.effective_equity(raw_now)
        if not (math.isfinite(old) and math.isfinite(new) and old > 0 and new > 0):
            log.error("capital basis unusable (old %r, new %r): references not re-based",
                      old, new)
            return
        if abs(new / old - 1.0) > 1e-9:
            self.engine.risk.rebase_equity(old, new)
            log.warning("capital settings changed while the engine was down (%s -> %s, "
                        "paper capital reset: %s): drawdown references scaled by %.6f",
                        saved_basis, self._capital_basis(),
                        self._startup_raw_equity is not None, new / old)

    def _persist_engine_state(self, bar_ts: datetime | None) -> None:
        """Save DecisionEngine.state_dict() as this mode's restart point. A
        failure is logged, not raised: the engine keeps running on its
        in-memory state and the previous save stays the restart point."""
        if not self._state_save_enabled:
            return
        bad: list[str] = []
        try:
            state = _json_safe(self.engine.state_dict(), "state", bad)
        except TypeError as e:
            log.error("engine state not saved: %s", e)
            return
        if bad:
            log.warning("engine state: %d non-finite value(s) saved as null, first: %s",
                        len(bad), bad[:3])
        key = self._engine_state_key
        sess = SessionLocal()
        try:
            row = sess.get(EngineState, key)
            if row is None:
                row = EngineState(mode=key)
                sess.add(row)
            row.saved_at = now_ist()
            row.bar_ts = bar_ts
            row.config_hash = self._config_hash()
            row.version = ENGINE_STATE_VERSION
            row.state = state
            row.basis = self._capital_basis()
            sess.commit()
        except SQLAlchemyError as e:
            sess.rollback()
            log.error("engine state not saved; a restart resumes from the previous save: %s", e)
        finally:
            sess.close()

    def _restore_engine_state(self) -> bool:
        """Resume from the state the previous run of this mode saved. Call
        after warm-up and daily-panel seeding. Returns True when restored.

        An unreadable save is left in place and no new save replaces it: it
        may hold a kill latch or a drawdown reference, and overwriting it with
        a fresh engine's state would erase them for good."""
        key = self._engine_state_key
        sess = SessionLocal()
        try:
            row = sess.get(EngineState, key)
            snap = None if row is None else (row.version, row.config_hash, row.saved_at,
                                              row.bar_ts, row.state, row.basis or {})
        except SQLAlchemyError as e:
            log.error("engine state unreadable (is the engine_state migration applied?): "
                      "starting from warm-up: %s", e)
            return False
        finally:
            sess.close()
        if snap is None:
            log.info("no saved %s engine state: starting from warm-up", key)
            return False
        version, config_hash, saved_at, bar_ts, state, basis = snap
        try:
            if version != ENGINE_STATE_VERSION:
                raise ValueError(f"version {version}, this build reads {ENGINE_STATE_VERSION}")
            # A throwaway engine takes the load first, so a bad save cannot
            # leave the live engine half restored.
            DecisionEngine(self.cfg, instruments=self.instruments).restore_state(
                copy.deepcopy(state))
            self.engine.restore_state(state)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            self._state_save_enabled = False
            log.error("saved %s engine state could not be restored (%s): starting from "
                      "warm-up and NOT saving over it; inspect engine_state", self.mode, e)
            notify_alert(self.publisher, severity="crit", kind="engine_state",
                         title="Saved engine state not restored",
                         body=f"{self.mode}: {e}. Kill latches and the drawdown reference "
                              "from before the restart are not in effect.")
            return False
        if config_hash != self._config_hash():
            log.warning("config changed since the %s engine state was saved; restored anyway",
                        key)
        self._rebase_to_current_capital(basis)
        log.info("%s engine state restored (saved %s, last bar %s)", key, saved_at, bar_ts)
        return True

    def _seed_marks_for_held(self) -> None:
        """Last recorded close for every held symbol warm-up did not price, so
        the book is never valued with a position missing."""
        missing = [s for s, p in self.broker.positions.items()
                   if p.qty and s not in self._last_prices]
        if not missing:
            return
        sess = SessionLocal()
        try:
            tf = self.cfg.engine.decision_bar_minutes
            for sym in missing:
                last = (sess.query(MarketBar.close)
                        .filter(MarketBar.symbol == sym, MarketBar.tf_minutes == tf)
                        .order_by(MarketBar.ts.desc()).limit(1).scalar())
                if last is not None and math.isfinite(last) and last > 0:
                    self._last_prices[sym] = float(last)
                    log.warning("held %s had no warm-up price; marked at its last "
                                "recorded close %.2f", sym, last)
                else:
                    log.error("held %s has no price at all; bars stay halted until "
                              "it prints", sym)
        except SQLAlchemyError as e:
            log.error("could not read last recorded closes for %s: %s", missing, e)
        finally:
            sess.close()

    # --------------------------------------------------------------- loop
    def _alert_on_decision(self, decision) -> None:
        """Raise an alert on a risk-state TRANSITION (kill priority over halt).
        Best-effort; notify_alert swallows its own errors."""
        kr = decision.kill_reason
        if kr and kr != self._last_kill:
            notify_alert(self.publisher, severity="crit", kind="kill",
                         title=f"KILL: {kr}",
                         body=f"{self.mode}: new risk blocked / positions flattened")
        elif not kr and decision.halted and not self._last_halted:
            notify_alert(self.publisher, severity="crit", kind="halted",
                         title="Engine halted (risk veto)",
                         body=f"{self.mode}: no new risk this bar")
        elif (not kr and not decision.halted
              and (self._last_kill or self._last_halted)):
            notify_alert(self.publisher, severity="info", kind="recovered",
                         title="Risk state cleared — engine running",
                         body=self.mode)
        self._last_kill = kr
        self._last_halted = decision.halted

    def _alert_on_feed(self, now: datetime) -> None:
        """Alert on a market-data feed stale/recovery transition during hours."""
        if self.feed is None or not is_session_open(now):
            return
        age = self.feed.seconds_since_tick()
        if age is not None:
            stale = age > FEED_STALE_S
        else:  # never ticked: stale only after a grace period from start
            stale = (time.monotonic() - self._started_monotonic) > FEED_STALE_S
        if stale and not self._feed_stale:
            notify_alert(self.publisher, severity="warn", kind="feed_stale",
                         title="Market-data feed stale",
                         body=f"{self.mode}: no ticks for "
                              f"{round(age) if age is not None else '∞'}s; "
                              "auto-reconnecting")
            self._feed_stale = True
        elif not stale and self._feed_stale:
            notify_alert(self.publisher, severity="info", kind="feed_ok",
                         title="Market-data feed recovered", body=self.mode)
            self._feed_stale = False

    def warmup_bars_needed(self) -> int:
        """Decision bars of history the engine needs at its first live bar:
        every enabled sleeve's lookback, the covariance window, and the regime
        model's full training window at its slower clock."""
        cfg = self.cfg
        needs = [WARMUP_MIN_BARS, cfg.engine.cov_window_bars + 1,
                 cfg.regime.timeframe_bars * (cfg.regime.train_window + 2)]
        needs += [s.warmup_bars() for s in self._all_strategies]
        return int(max(needs))

    def _warmup_from_history(self, lookback_bars: int | None = None,
                             decide_bars: int = WARMUP_DECIDE_BARS) -> None:
        """Replay recent Angel history so the histories, the regime model and
        the edge statistics are warm at the first live bar (without it the
        engine started blind and took no trades for days, 2026-06-15). The
        newest ``decide_bars`` bars run through post_bar and decide (NO
        execution); older ones only fill the histories. Best-effort: it never
        blocks the engine start.

        The window is sized in sessions of the configured clock. It used to
        assume 75 bars a session (the 5-minute clock), so on 15-minute bars
        it fetched about 900 bars: the regime HMM, which needs 1,500 to fit
        at all, never fitted live, and the pairs sleeve never had its
        lookback, while the backtest ran both.

        Replayed decisions must not leave risk state behind: they mark today's
        book at historical prices, so a replayed peak became the drawdown
        reference and a replayed dip could latch a kill. The risk engine and
        the tier ladder are put back as they were before the replay.
        """
        bar_min = self.cfg.engine.decision_bar_minutes
        interval = _ANGEL_INTERVAL.get(bar_min)
        if interval is None:
            log.warning("warmup: no Angel interval for %d-min bars; skipping", bar_min)
            return
        lookback_bars = lookback_bars or self.warmup_bars_needed()
        sessions = math.ceil(lookback_bars / bars_per_day(bar_min))
        days = math.ceil(sessions * _CALENDAR_PER_SESSION) + 7
        end = now_ist()
        start = end - timedelta(days=days)
        # a candle whose window has not closed yet is still changing
        complete_before = end - timedelta(minutes=bar_min)
        per_sym: dict[str, list] = {}
        for sym in self.instruments:
            try:
                rows = self.broker_adapter.historical_candles(sym, interval, start, end)
            except Exception as e:
                log.warning("warmup fetch %s failed: %s", sym, e)
                continue
            rows = [r for r in rows if r[0] <= complete_before]
            if rows:
                per_sym[sym] = rows[-lookback_bars:]
        if not per_sym:
            log.warning("warmup: no history fetched — engine starts cold")
            return
        short = sorted(s for s, rows in per_sym.items() if len(rows) < lookback_bars)
        if short:
            log.warning("warmup: %d of %d instruments have fewer than %d bars (%s...)",
                        len(short), len(per_sym), lookback_bars, short[:5])

        # One broker read for the whole replay: the held book does not change
        # while replaying, and on the live broker every re-read is a call
        # that can fail and would abort the warm-up.
        positions = {s: Position(s, p.qty, getattr(p, "avg_price", 0.0))
                     for s, p in self.broker.positions.items()}
        cash = float(self.broker.cash)

        def replay_equity() -> float:
            mtm = 0.0
            for s, p in positions.items():
                px, inst = self._last_prices.get(s), self.instruments.get(s)
                if px is not None and inst is not None:
                    mtm += p.qty * px * inst.point_value
            return self.effective_equity(cash + mtm)

        risk_before = copy.deepcopy(self.engine.risk.state_dict())
        ladder_before = copy.deepcopy(self.engine.ladder.state_dict())
        # merge per-symbol candles into time-ordered batches (point-in-time)
        all_ts = sorted({r[0] for rows in per_sym.values() for r in rows})
        session_ts = [ts for ts in all_ts if is_session_open(ts)]
        decide_from = session_ts[-decide_bars] if len(session_ts) > decide_bars else None
        idx = dict.fromkeys(per_sym, 0)
        n = 0
        try:
            for ts in all_ts:
                for s, rows in per_sym.items():
                    i = idx[s]
                    if i < len(rows) and rows[i][0] == ts:
                        _, o, h, lo, c, v = rows[i]
                        self.histories[s].append(Bar(ts=ts, open=o, high=h, low=lo,
                                                     close=c, volume=v))
                        self._last_prices[s] = c
                        idx[s] = i + 1
                if not is_session_open(ts) or (decide_from is not None and ts < decide_from):
                    continue
                state = MarketState(
                    ts=ts, equity=replay_equity(), bars=self.histories,
                    instruments=self.instruments, positions=positions,
                )
                self.engine.post_bar(state)
                self.engine.decide(state)  # warms stats/regime; orders NOT executed
                n += 1
        finally:
            self.engine.risk.load_state(risk_before)
            self.engine.ladder.load_state(ladder_before)
        log.info("warmup: %d bars of history, %d decided, across %d instruments",
                 len(all_ts), n, len(per_sym))

    def _seed_daily_panels(self) -> None:
        """Give every panel sleeve (factor / reversal / downshock — anything
        exposing ``seed_daily``) its daily history: none of them can derive
        months of daily closes from live intraday bars, so 'enabled' without
        this would silently mean 'no-op for a year'. One broker fetch per
        EQUITY symbol, fanned out to every consumer; best-effort per symbol
        (a missing name just drops out of the breadth count)."""
        sleeves = [s for s in self.engine.strategies if hasattr(s, "seed_daily")]
        if not sleeves:
            return
        days = 400
        for s in sleeves:
            if hasattr(s, "seed_days_needed"):
                days = max(days, int(s.seed_days_needed()))
        end = now_ist()
        start = end - timedelta(days=days)
        today = end.date()
        n = 0
        for sym, inst in self.instruments.items():
            if inst.kind != InstrumentKind.EQUITY:
                continue
            try:
                rows = self.broker_adapter.historical_candles(sym, "ONE_DAY", start, end)
            except Exception as e:
                log.warning("daily panel seed %s failed: %s", sym, e)
                continue
            # A restart during the session can be handed today's candle while
            # it is still forming. Seeded as a finished day, it stood in for
            # the real row: the panel roll keeps an existing row for a date,
            # so today's full row was dropped and a shock later in the day
            # was never seen. Today's row is built from live bars instead.
            rows = [r for r in rows if _row_date(r[0]) < today]
            if rows:
                for s in sleeves:
                    s.seed_daily(sym, rows)
                n += 1
        # Warmup (run just before this) drove decide() on the still-empty
        # panels, advancing any cadence counters; tell the sleeves seeding is
        # done so cadence-based ones (factor) rebalance on the next live bar
        # instead of staying a no-op until the counter rolls over.
        for s in sleeves:
            try:
                s.on_seeded()
            except Exception as e:  # pragma: no cover - defensive
                log.warning("on_seeded %s failed: %s", s.name, e)
        log.info("daily panels seeded for %d equities across %s",
                 n, [s.name for s in sleeves])
        # Depth telemetry: a cross-sectional sleeve that silently receives too
        # few rows (fewer calendar days fetched than its lookback needs) stays
        # breadth-gated and never trades — surface it instead of hiding it.
        for s in sleeves:
            panel = getattr(s, "_panel", None)
            if panel:
                depths = sorted(len(dq) for dq in panel.values())
                log.info("panel[%s]: %d names, rows min/med/max=%d/%d/%d",
                         s.name, len(depths), depths[0],
                         depths[len(depths) // 2], depths[-1])

    def _resume_engine(self) -> None:
        """After warm-up and seeding: restore the previous run's engine state,
        drop stop state warm-up left on symbols not held, price every held
        symbol, and save a restart point. Without the restore the 08:50
        recycle cleared the kill latches, re-based drawdown to that morning,
        cut every down-shock hold to one session and made the monthly factor
        rebalance run daily."""
        held: dict[str, float] | None
        try:
            held = {s: float(p.qty) for s, p in self.broker.positions.items()}
        except BrokerError as e:
            held = None
            log.error("positions unreadable at start (%s); warm-up stop state kept", e)
        if held is not None:
            self._seed_marks_for_held()        # before the re-base values the book
        restored = self._restore_engine_state()
        # A restored engine's stops and cooldowns are the previous run's real
        # ones (a cooldown sits on a symbol just stopped out, i.e. flat), so
        # only a cold start drops what warm-up left behind.
        if held is not None and not restored:
            self.engine.end_warmup(held)
        self._persist_engine_state(None)

    def _seed_equity_snapshot(self) -> None:
        """Write one equity point at startup so the dashboard shows starting
        capital straight away instead of 'no data' until the first bar
        completes (up to a full bar after a restart)."""
        try:
            prices = self.current_prices()
            gross, net = self.broker.exposures(prices)
            self.recorder.record_equity(
                now_ist(), equity=self.broker.equity(prices),
                cash=self.broker.cash, mtm=self.broker.mtm(prices),
                gross=gross, net=net, realized=self.broker.realized_total,
                unrealized=self.broker.unrealized(prices),
            )
        except Exception as e:  # pragma: no cover - best-effort seed
            log.warning("initial equity snapshot failed: %s", e)

    def _absorb_bars(self, batch: dict[str, Bar]) -> None:
        """Record bars into history WITHOUT deciding — for stragglers that
        arrive after their bucket was already decided (or out-of-session
        buckets, e.g. the pre-open aggregation of a stale overnight bar)."""
        for sym, bar in batch.items():
            h = self.histories.get(sym)
            if h is not None:
                h.append(bar)
        self.recorder.record_bars(self.cfg.engine.decision_bar_minutes, batch)

    def _poll_once(self, now: datetime, decide_grace_s: float = 3.0) -> bool:
        """One poll-loop iteration: complete elapsed bars, accumulate, and run
        at most ONE decision — for the bucket, at the bucket timestamp, on the
        full cross-section. Returns True when a decision step ran.

        Bars used to complete per-symbol on the next tick and the old loop
        stepped on every non-empty drain: 3–5 decisions per bar, each on a
        partial universe (the churn + duplicate-decision bug), and no
        close-bar decision at all (nothing crosses 15:30).
        """
        self.aggregator.flush_older(now)
        for sym, bar in self._drain_pending().items():
            self._bar_accum[sym] = bar
        if not self._bar_accum:
            return False

        bucket = max(b.ts for b in self._bar_accum.values())
        older = {s: b for s, b in self._bar_accum.items() if b.ts < bucket}
        if older:
            self._absorb_bars(older)
            self._bar_accum = {s: b for s, b in self._bar_accum.items()
                               if b.ts >= bucket}
        due = now >= bucket + timedelta(minutes=self.cfg.engine.decision_bar_minutes,
                                        seconds=decide_grace_s)
        if not (due and self._bar_accum):
            return False
        batch, self._bar_accum = self._bar_accum, {}
        already = (self._last_step_bucket is not None
                   and bucket <= self._last_step_bucket)
        if already or not is_session_open(bucket):
            self._absorb_bars(batch)
            return False
        # ONE decision per bucket, stamped at the bar ts — the backtest
        # convention, so live and backtest share the same clock.
        self._last_step_bucket = bucket
        self.step(bucket, batch)
        return True

    def run_forever(self, poll_seconds: float = 1.0,
                    decide_grace_s: float = 3.0) -> None:
        log.info("LiveRunner started mode=%s instruments=%d", self.mode,
                 len(self.instruments))
        self._started_monotonic = time.monotonic()
        try:
            self._warmup_from_history()
        except Exception as e:  # best-effort — a cold start is still functional
            log.warning("warmup failed (engine starts cold): %s", e)
        try:
            self._seed_daily_panels()
        except Exception as e:  # panel sleeves then stay breadth-gated no-ops
            log.warning("daily panel seed failed: %s", e)
        self._resume_engine()
        self._seed_equity_snapshot()
        notify_alert(self.publisher, severity="info", kind="engine_start",
                     title=f"Engine started ({self.mode})",
                     body=f"instruments={len(self.instruments)} "
                          "data_source=angelone")
        self._last_heartbeat_mono = 0.0
        self._loop_errors = 0
        while True:
            self._guarded_iteration(decide_grace_s)
            time.sleep(poll_seconds)

    def _guarded_iteration(self, decide_grace_s: float) -> None:
        """One poll-loop tick, guarded. A transient failure — most importantly a
        momentary Postgres/DNS blip on a recorder write (`failed to resolve host
        'postgres'`, seen 2026-07-07) — must NOT propagate out of run_forever and
        exit the process: `unless-stopped` then restarts the container, re-running
        warmup + daily-panel seeding and throwing away warm sleeve/edge state over
        a hiccup that pool_pre_ping would have healed on the next tick. So log it
        and retry next poll instead. KeyboardInterrupt/SystemExit still stop us."""
        try:
            now = now_ist()
            stepped = self._poll_once(now, decide_grace_s)
            if not stepped:
                # idle (closed market or bucket not due): heartbeat + commands
                if isinstance(self.broker, LiveExecutionBroker):
                    self.broker.pump(self.current_prices())
                if time.monotonic() - self._last_heartbeat_mono > 10:
                    feed_age = self.feed.seconds_since_tick() if self.feed else None
                    self.recorder.heartbeat(
                        status=("paused" if self.paused else
                                "idle" if not is_session_open(now) else "running"),
                        market_open=is_session_open(now),
                        detail={"venue": self.mode, "data_source": "angelone",
                                "feed_age_s": (round(feed_age, 1)
                                               if feed_age is not None else None)})
                    self._alert_on_feed(now)
                    self._last_heartbeat_mono = time.monotonic()
                self._poll_commands(None)
            self._loop_errors = 0
        except (KeyboardInterrupt, SystemExit):
            raise
        except Exception as e:
            self._loop_errors = getattr(self, "_loop_errors", 0) + 1
            log.error("engine loop tick failed (#%d) — retrying next poll, not "
                      "crashing the engine: %s", self._loop_errors, e)


def _row_date(ts) -> date:
    """Session date of a candle stamp (datetime, date or ISO string)."""
    if isinstance(ts, datetime):
        return ts.date()
    if isinstance(ts, date):
        return ts
    return date.fromisoformat(str(ts)[:10])


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="config/base.yaml")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--paper", action="store_true", help="paper on live data (default)")
    g.add_argument("--live", action="store_true", help="REAL MONEY (gated)")
    ap.add_argument("--capital", type=float, default=1_000_000.0)
    args = ap.parse_args()

    mode = "live" if args.live else "paper"
    runner = LiveRunner(args.config, mode=mode, paper_capital=args.capital)

    # start the websocket feed
    adapter = runner.broker_adapter
    from quantsys.execution.marketdata import AngelWebSocketFeed

    tokens_by_exchange: dict[str, list[str]] = {}
    token_to_symbol: dict[str, str] = {}
    for sym, inst in runner.instruments.items():
        if inst.token:
            tokens_by_exchange.setdefault(inst.exchange, []).append(inst.token)
            token_to_symbol[inst.token] = sym
    feed = AngelWebSocketFeed(
        auth_token=getattr(adapter._transport, "access_token", ""),
        api_key=adapter.api_key, client_code=adapter.client_code,
        feed_token=adapter._feed_token, tokens_by_exchange=tokens_by_exchange,
        aggregator=runner.aggregator, token_to_symbol=token_to_symbol,
        reauth=adapter.reconnect_feed_session,
    )
    runner.feed = feed
    feed.start()
    try:
        runner.run_forever()
    except KeyboardInterrupt:
        runner.recorder.heartbeat(status="stopped", market_open=False,
                                  detail={"reason": "live runner exit"})
        feed.stop()


if __name__ == "__main__":
    main()
