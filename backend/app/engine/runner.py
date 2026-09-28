"""Trading engine orchestration.

    market data → technical analysis → strategy → risk engine → (AI confirmation)
      → trade decision → execution engine → venue → position monitor → notify + DB

Only one engine process may run (PostgreSQL advisory lock, see ``main``). Every
loop is individually guarded: an exception in one never stops the others, and a
failure inside the decision pipeline always results in NO TRADE.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import __version__
from app.ai.context import build_context
from app.ai.service import AIService
from app.config import Settings
from app.core import params as P
from app.core.store import Store
from app.db import models as M
from app.db.types import utcnow
from app.exchange.base import ExecutionAdapter
from app.exchange.binance_rest import BinanceFuturesAdapter, BinancePublic
from app.exchange.credentials import check_credentials, credential_status, store_credential_status
from app.exchange.models import ExchangeError
from app.exchange.paper import PaperExchange
from app.execution.engine import ExecutionEngine, TradePlan
from app.execution.monitor import PositionMonitor, Reconciler
from app.market.data_service import MarketDataService
from app.market.features import TFView, compute_features
from app.notifications.service import NotificationService
from app.risk.engine import AccountState, RiskEngine, SystemGates, TradeStats
from app.strategy.base import Evaluation, MarketExtras, StrategyState
from app.strategy.breakout_retest import BreakoutRetestStrategy

log = logging.getLogger("kestrel.engine")


@dataclass
class Leg:
    """Everything bound to one execution adapter (paper or live)."""

    adapter: ExecutionAdapter
    execu: ExecutionEngine
    monitor: PositionMonitor
    reconciler: Reconciler
    reconciled: bool = False
    last_reconcile: float = 0.0
    monitor_failures: int = 0


@dataclass
class Guards:
    reasons: dict[str, str] = field(default_factory=dict)

    def set(self, key: str, reason: str) -> bool:
        new = key not in self.reasons
        self.reasons[key] = reason
        return new

    def clear(self, key: str) -> bool:
        return self.reasons.pop(key, None) is not None


class Engine:
    def __init__(self, settings: Settings, store: Store, notifier: NotificationService, market: MarketDataService,
                 public: BinancePublic, ai: AIService, live_adapter: BinanceFuturesAdapter | None) -> None:
        self.s = settings
        self.store = store
        self.notifier = notifier
        self.market = market
        self.public = public
        self.ai = ai
        self.live_adapter = live_adapter
        self.strategy = BreakoutRetestStrategy()
        self.strategy_state = StrategyState()
        self.guards = Guards()
        self.trading: dict[str, Any] = {}
        self.safety: dict[str, Any] = P.safety_defaults()
        self.cfg: M.StrategyConfig | None = None
        self.params: dict[str, Any] = P.defaults()
        self.paper: PaperExchange | None = None
        self.legs: dict[str, Leg] = {}
        self.eval_lock = asyncio.Lock()
        self.last_eval: Evaluation | None = None
        self.last_regime: str | None = None
        self.regime_candidate: tuple[str, int] | None = None
        self.last_regime_ai = 0.0
        self.last_periodic_ai = time.time()
        self.last_review_ai = 0.0
        self.clock_skew_ms: int | None = None
        self.started_at = time.time()
        self.stale_since: float | None = None
        self.cred: dict[str, Any] = {"status": "missing"}
        self._stop = asyncio.Event()
        self._tasks: list[asyncio.Task[Any]] = []
        self._last_mode: str | None = None
        self._salt = "x0"

    # ------------------------------------------------------------------ setup
    async def start(self) -> None:
        self.trading = await self.store.trading_state()
        self.safety = await self.store.safety()
        self.cfg, self.params = await self.store.active_params()
        meta = await self.store.get_setting("install", {})
        if not meta.get("salt"):
            import secrets

            meta["salt"] = secrets.token_hex(2)
            await self.store.set_setting("install", meta)
        self._salt = meta["salt"]

        # Paper venue (state persisted in settings so it survives restarts)
        async def persist(snap: dict[str, Any]) -> None:
            await self.store.set_setting("paper_state", snap)

        self.paper = PaperExchange(self.market, starting_balance=self.s.paper_starting_balance, symbol=self.s.symbol,
                                   taker_fee_pct=self.params["taker_fee_pct"], slippage_bps=self.params["paper_slippage_bps"],
                                   stop_slippage_bps=self.params["paper_stop_slippage_bps"], persist=persist)
        snap = await self.store.get_setting("paper_state", {})
        if snap.get("wallet_balance") is not None:
            self.paper.restore(snap)
        else:
            await persist(self.paper.snapshot())
        self.legs["paper"] = self._make_leg(self.paper)
        if self.live_adapter is not None:
            try:
                self.live_adapter._filters.clear()  # noqa: SLF001 - start with fresh filters
            except AttributeError:
                pass
            self.legs["live"] = self._make_leg(self.live_adapter)

        await self.store.set_component("engine", "ok", f"starting v{__version__}")
        await self.market.start()
        self.market.on_kline_closed(self._on_kline)
        try:
            p_filters = await self.public.filters(self.s.symbol)
            self.paper.filters = p_filters
        except ExchangeError as e:
            log.warning("could not load symbol filters, using defaults: %s", e)
        await self._check_credentials()
        await self._reconcile("paper", startup=True)
        if self.trading["mode"] == "live" or await self.store.open_trades("live"):
            await self._reconcile("live", startup=True)
        self._last_mode = self.trading["mode"]
        await self.store.system_event("info", "engine", "started", f"engine v{__version__} started in "
                                      f"{self.trading['mode'].upper()} mode")
        loops = [
            ("state", self._state_loop, 1.0),
            ("commands", self._command_loop, 1.0),
            ("paper-monitor", self._paper_monitor_loop, 1.0),
            ("live-monitor", self._live_monitor_loop, 3.0),
            ("supervisor", self._supervisor_loop, 5.0),
            ("publisher", self._publish_loop, 1.0),
            ("slow", self._slow_loop, 60.0),
            ("ai", self._ai_loop, 30.0),
            ("heartbeat", self._heartbeat_loop, 5.0),
        ]
        self._tasks = [asyncio.create_task(self._guarded(name, fn, period), name=name) for name, fn, period in loops]

    def _make_leg(self, adapter: ExecutionAdapter) -> Leg:
        async def halt(reason: str) -> None:
            await self.halt(reason)

        execu = ExecutionEngine(adapter, self.store, self.notifier, self.s.symbol, halt, salt=self._salt)
        monitor = PositionMonitor(execu, self.store, self.notifier, halt, on_closed=self._on_trade_closed)
        return Leg(adapter, execu, monitor, Reconciler(execu, monitor, self.store, self.notifier, halt))

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.market.stop()

    async def wait(self) -> None:
        await self._stop.wait()

    async def _guarded(self, name: str, fn, period: float) -> None:  # noqa: ANN001
        errors = 0
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                await fn()
                errors = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                errors += 1
                log.exception("loop %s failed", name)
                if errors in (3, 30):
                    await self.store.system_event("error", "engine", f"loop_{name}_failing",
                                                  f"engine loop '{name}' failing repeatedly: {type(e).__name__}: {e}")
            await asyncio.sleep(max(0.05, period - (time.monotonic() - t0)) if errors == 0 else min(30, period * (1 + errors)))

    # ------------------------------------------------------------------ state
    @property
    def mode(self) -> str:
        return self.trading.get("mode", "paper")

    def leg(self, mode: str | None = None) -> Leg | None:
        return self.legs.get(mode or self.mode)

    async def _state_loop(self) -> None:
        self.trading = await self.store.trading_state()
        if self._last_mode != self.mode:
            prev, self._last_mode = self._last_mode, self.mode
            await self.store.system_event("warning", "engine", "mode_changed", f"trading mode {prev} → {self.mode}")
            if self.mode == "live":
                leg = self.leg("live")
                if leg:
                    leg.reconciled = False
                    await self._reconcile("live", startup=True)

    async def halt(self, reason: str) -> None:
        if self.trading.get("halted") and self.trading.get("halt_reason") == reason:
            return
        self.trading = await self.store.update_trading({"halted": True, "halt_reason": reason,
                                                        "halted_at": utcnow().isoformat()})
        await self.store.system_event("critical", "safety", "halt", f"TRADING HALTED: {reason}")
        await self.store.risk_event("halt", "critical", reason)
        await self.notifier.notify("risk", "Trading halted", f"{reason}. New trades disabled until you re-enable "
                                   "trading.", "critical", {"url": "/system"})

    def gates(self) -> SystemGates:
        g = SystemGates(kill_switch=bool(self.trading.get("kill_switch")),
                        strategy_enabled=bool(self.trading.get("strategy_enabled")),
                        halted=bool(self.trading.get("halted")), halt_reason=self.trading.get("halt_reason"),
                        guard_reasons=list(self.guards.reasons.values()))
        if self.mode == "live":
            leg = self.leg("live")
            if leg is None:
                g.guard_reasons.append("live mode but no Binance credentials")
            elif not leg.reconciled:
                g.guard_reasons.append("live reconciliation pending")
            if self.cred.get("status") != "connected":
                g.guard_reasons.append(f"Binance credentials {self.cred.get('status')}")
        return g

    # ------------------------------------------------------------------ evaluation
    async def _on_kline(self, tf: str, row: dict[str, Any]) -> None:
        if tf == "5m":
            asyncio.create_task(self._evaluate_safe(row))

    async def _evaluate_safe(self, row: dict[str, Any]) -> None:
        try:
            await self.evaluate_cycle()
        except Exception as e:  # noqa: BLE001 - failure in the pipeline ⇒ no trade
            log.exception("evaluation cycle failed")
            await self.store.system_event("error", "strategy", "evaluation_failed", f"evaluation failed: {e}")
            await self.store.set_component("strategy", "degraded", f"evaluation failed: {type(e).__name__}")

    def _views(self) -> dict[str, TFView]:
        views = {}
        for tf in ("1m", "5m", "15m", "1h", "4h"):
            s = self.market.series(tf)
            views[tf] = TFView(s, compute_features(s, self.params), len(s) - 1)
        return views

    def _extras(self) -> MarketExtras:
        m = self.market
        return MarketExtras(funding_rate=m.funding_rate, next_funding_ms=m.next_funding_ms, open_interest=m.open_interest,
                            oi_change_1h_pct=m.oi_change_1h_pct(), spread_bps=m.spread_bps(), mark_price=m.mark_price(),
                            liquidations_1h=m.liquidations_1h())

    async def evaluate_cycle(self) -> Evaluation | None:
        async with self.eval_lock:
            await asyncio.sleep(1.5)  # let the 15m/1h/4h closes that coincide with this 5m close arrive
            for tf in ("5m", "15m", "1h", "4h"):
                await self.market.ensure_fresh(tf)
            self.cfg, self.params = await self.store.active_params()
            if self.paper:
                self.paper.configure(self.params["taker_fee_pct"], self.params["paper_slippage_bps"],
                                     self.params["paper_stop_slippage_bps"])
            views = self._views()
            extras = self._extras()
            ev = self.strategy.evaluate(views, extras, self.params, self.strategy_state)
            self.last_eval = ev
            await self.store.publish("evaluation", {**ev.to_dict(), "strategy_version": self.cfg.version,
                                                    "evaluated_at": utcnow().isoformat()})
            await self.store.set_component("strategy", "ok", ev.summary[:300],
                                           {"last_eval": ev.bar_time, "decision": ev.decision})
            snap = self.market.snapshot()
            async with self.store.factory() as s:
                s.add(M.MarketSnapshot(price=snap["price"] or ev.price, mark_price=snap["mark_price"],
                                       funding_rate=snap["funding_rate"], open_interest=snap["open_interest"],
                                       change_24h_pct=snap["change_24h_pct"], volume_24h=snap["volume_24h"],
                                       regime=ev.regime, data={"timeframes": ev.timeframes, "decision": ev.decision,
                                                               "levels": ev.levels}))
                await s.commit()
            await self._regime_tracking(ev, views, extras)
            if ev.setup:
                await self.handle_setup(ev, views, extras)
            return ev

    async def handle_setup(self, ev: Evaluation, views: dict[str, TFView], extras: MarketExtras) -> None:
        setup = ev.setup
        if setup is None:
            return
        mode = self.mode
        leg = self.leg(mode)
        p = self.params
        self.strategy_state.last_signal_bar[setup.direction] = ev.bar_time
        sig = await self.store.create_signal(
            bar_time=ev.bar_time, mode=mode, strategy_config_id=self.cfg.id if self.cfg else None,
            strategy_version=self.cfg.version if self.cfg else None, direction=setup.direction, status="candidate",
            confidence=setup.confidence, entry=setup.entry, stop=setup.stop, tp1=setup.tp1, tp2=setup.tp2, rr=setup.rr,
            reasons=setup.reasons + [f"⚠ {w}" for w in setup.warnings],
            context={"regime": ev.regime, "timeframes": ev.timeframes, "levels": ev.levels,
                     "long_checks": [c.to_dict() for c in ev.long_checks],
                     "short_checks": [c.to_dict() for c in ev.short_checks]})
        await self.notifier.notify("signal", f"BTC potential {setup.direction} setup",
                                   f"BTC potential {setup.direction} setup detected · confidence "
                                   f"{setup.confidence:.0f} · R:R {setup.rr:.1f}", "info", {"url": "/signals"})
        pre: list[tuple[str, str, dict[str, Any]]] = [
            ("signal_generated", f"{setup.direction} setup at {setup.entry:,.1f} (confidence {setup.confidence:.0f})",
             {"signal_id": sig.id}),
            ("strategy_approved", "; ".join(setup.reasons), {"checks": len(ev.long_checks if setup.direction == 'LONG' else ev.short_checks)}),
        ]
        if leg is None:
            await self.store.update_signal(sig.id, status="rejected_risk", rejection="no execution venue for mode")
            return
        # --- risk ------------------------------------------------------------------
        try:
            acct = await leg.adapter.get_account()
            filters = await leg.adapter.get_filters(self.s.symbol)
            stats = TradeStats(**await self.store.trade_stats(mode, utcnow(), acct.equity))
            try:
                pos = await leg.adapter.get_position(self.s.symbol)
                if pos is not None:
                    stats.open_positions = max(stats.open_positions, 1)
                    stats.has_open_trade = True
            except ExchangeError:
                stats.has_open_trade = True  # unknown ⇒ assume the worst
            decision = RiskEngine(p).evaluate(setup, AccountState(acct.equity, acct.available_balance,
                                                                  acct.wallet_balance),
                                              stats, self.gates(), filters, extras.funding_rate, extras.spread_bps,
                                              utcnow(), extras.next_funding_ms)
        except Exception as e:  # noqa: BLE001 - "risk calculations fail" ⇒ halt
            log.exception("risk evaluation failed")
            await self.store.update_signal(sig.id, status="rejected_risk", rejection=f"risk calculation failed: {e}")
            await self.halt(f"risk calculation failed: {type(e).__name__}")
            return
        await self.store.update_signal(sig.id, risk_result=decision.to_dict())
        if not decision.approved or decision.sizing is None:
            await self.store.update_signal(sig.id, status="rejected_risk", rejection="; ".join(decision.blocked_by)[:2000])
            log.info("setup rejected by risk", extra={"blocked_by": decision.blocked_by})
            return
        sz = decision.sizing
        pre.append(("risk_approved", f"{sz.quantity} BTC at {sz.leverage}×, risk {sz.risk_usdt:.2f} USDT "
                                     f"({sz.risk_pct:.2f}%)", sz.to_dict()))
        # --- AI ---------------------------------------------------------------------
        recent = await self.store.recent_closed_trades(mode, 5)
        ctx = build_context(ev, views, extras, self.market.snapshot(), acct, None, recent, decision.to_dict(), mode)
        gate = await self.ai.confirm_setup(ctx, setup.direction, ev.bar_time, p)
        await self.store.update_signal(sig.id, ai_analysis_id=gate.analysis_id)
        pre.append(("ai_analyzed", gate.reason, {"decision": gate.decision, "confidence": gate.confidence,
                                                 "analysis_id": gate.analysis_id, "degraded": gate.degraded}))
        if not gate.allowed:
            status = "ai_unavailable" if gate.degraded else "rejected_ai"
            await self.store.update_signal(sig.id, status=status, rejection=gate.reason)
            return
        # --- re-check gates (the kill switch may have been pressed during the AI call) ---
        self.trading = await self.store.trading_state()
        g = self.gates()
        if g.kill_switch or g.halted or not g.strategy_enabled or g.guard_reasons or self.mode != mode:
            await self.store.update_signal(sig.id, status="rejected_risk",
                                           rejection="trading state changed during analysis")
            return
        await self.store.update_signal(sig.id, status="approved")
        views5 = views["5m"]
        s5 = views5.series
        lo = max(0, views5.idx - 120)
        snapshot = {"candles_5m": [[int(s5.open_time[i]), float(s5.open[i]), float(s5.high[i]), float(s5.low[i]),
                                    float(s5.close[i])] for i in range(lo, views5.idx + 1)],
                    "levels": ev.levels, "timeframes": ev.timeframes, "level": setup.level}
        plan = TradePlan(
            signal_id=sig.id, direction=setup.direction, entry=setup.entry, stop=setup.stop, tp1=setup.tp1,
            tp2=setup.tp2, quantity=sz.quantity, tp1_qty=sz.tp1_qty, tp2_qty=sz.tp2_qty, split=sz.split,
            leverage=sz.leverage, risk_usdt=sz.risk_usdt, equity=acct.equity, max_slippage_bps=p["max_slippage_bps"],
            min_rr=p["min_rr"], tp_failure_policy=p["tp_failure_policy"],
            strategy_config_id=self.cfg.id if self.cfg else None, strategy_version=self.cfg.version if self.cfg else None,
            confidence=setup.confidence, ai_decision=gate.decision, ai_confidence=gate.confidence,
            market_regime=gate.market_regime or ev.regime, reasons=setup.reasons + ([gate.reason] if gate.used_ai else []),
            snapshot=snapshot, breakeven_after_tp1=bool(p["breakeven_after_tp1"]), pre_events=pre)
        result = await leg.execu.open_trade(plan)
        await self.store.update_signal(sig.id, status="executed" if result.ok else "execution_failed",
                                       trade_id=result.trade_id, rejection=None if result.ok else result.message)
        if not result.ok:
            log.warning("execution did not open trade: %s", result.message)
            await self.store.system_event("warning", "execution", "entry_aborted", result.message,
                                          {"signal_id": sig.id, "trade_id": result.trade_id})

    async def _regime_tracking(self, ev: Evaluation, views: dict[str, TFView], extras: MarketExtras) -> None:
        if self.last_regime is None:
            self.last_regime = ev.regime
            return
        if ev.regime == self.last_regime:
            self.regime_candidate = None
            return
        n = (self.regime_candidate[1] + 1) if self.regime_candidate and self.regime_candidate[0] == ev.regime else 1
        self.regime_candidate = (ev.regime, n)
        if n < 2:
            return
        prev, self.last_regime, self.regime_candidate = self.last_regime, ev.regime, None
        await self.store.system_event("info", "strategy", "regime_change", f"market regime {prev} → {ev.regime}")
        p = self.params
        if p["ai_regime_trigger"] and time.time() - self.last_regime_ai > p["ai_regime_cooldown_min"] * 60:
            self.last_regime_ai = time.time()
            ctx = build_context(ev, views, extras, self.market.snapshot(), None, None,
                                await self.store.recent_closed_trades(self.mode, 5), None, self.mode)
            asyncio.create_task(self.ai.analyze("regime_change", ctx, p,
                                                f"The deterministic regime changed from {prev} to {ev.regime}. "
                                                "Give a market read. decision=NO_TRADE unless a clear bias exists; "
                                                "recommended_action=WAIT."))

    # ------------------------------------------------------------------ monitors
    async def _paper_monitor_loop(self) -> None:
        if not self.paper:
            return
        events = await self.paper.on_market(self.market.funding_rate, self.market.next_funding_ms)
        for e in events:
            log.info("paper venue event", extra={"event": e})
        leg = self.legs["paper"]
        open_ = await self.store.open_trades("paper")
        if open_ or events or self.paper.state.position.quantity:
            await leg.monitor.run_once(self.safety)
        if time.time() - leg.last_reconcile > 300:
            await self._reconcile("paper")

    async def _live_monitor_loop(self) -> None:
        leg = self.legs.get("live")
        if leg is None:
            return
        active = self.mode == "live" or bool(await self.store.open_trades("live"))
        if not active:
            return
        try:
            await leg.monitor.run_once(self.safety)
            if leg.monitor_failures >= 3:
                await self.notifier.notify("system", "Binance connection restored", "Live position monitoring resumed.")
            leg.monitor_failures = 0
            await self.store.set_component("binance", "ok", "live monitoring OK")
        except ExchangeError as e:
            leg.monitor_failures += 1
            await self.store.set_component("binance", "degraded", f"monitor failing: {e}")
            if leg.monitor_failures == 3:
                await self.notifier.notify("system", "Binance connection lost",
                                           f"Live position monitoring failing ({e}). Exchange-side stop-loss and "
                                           "take-profit orders remain active.", "critical")
        if time.time() - leg.last_reconcile > 60:
            await self._reconcile("live")

    async def _reconcile(self, mode: str, startup: bool = False) -> None:
        leg = self.legs.get(mode)
        if leg is None:
            return
        leg.last_reconcile = time.time()
        try:
            report = await leg.reconciler.run(self.safety)
            leg.reconciled = True
            await self.store.publish(f"reconcile_{mode}", report)
            if mode == "live":
                await self.store.mark_readiness("reconciliation", True, "; ".join(report["actions"])[:300] or "clean")
            if report["actions"] and (startup or any("cancel" in a or "settled" in a or "adopt" in a
                                                     for a in report["actions"])):
                await self.store.system_event("warning" if report["actions"] else "info", "reconcile",
                                              f"reconcile_{mode}", f"{mode} reconciliation: " + "; ".join(report["actions"]),
                                              report)
        except ExchangeError as e:
            leg.reconciled = False if mode == "live" else leg.reconciled
            await self.store.system_event("warning", "reconcile", f"reconcile_{mode}_failed", f"{mode} reconciliation "
                                          f"failed: {e}")
            if mode == "live":
                await self.store.mark_readiness("reconciliation", False, str(e)[:300])

    async def _on_trade_closed(self, trade: M.Trade) -> None:
        """Post-close risk bookkeeping: loss-limit alerts."""
        p = self.params
        try:
            leg = self.leg(trade.mode)
            acct = await leg.adapter.get_account() if leg else None
            equity = acct.equity if acct else (trade.equity_at_entry or 0)
            st = await self.store.trade_stats(trade.mode, utcnow(), equity)
        except Exception:  # noqa: BLE001
            return
        flags = await self.store.get_setting("risk_alerts", {})
        today = utcnow().date().isoformat()
        if st["day_start_equity"] and -st["realized_today"] / st["day_start_equity"] * 100 >= p["max_daily_loss_pct"]:
            if flags.get("daily") != today:
                flags["daily"] = today
                await self.store.risk_event("daily_loss_limit", "critical", "daily loss limit reached")
                await self.notifier.notify("risk", "Daily loss limit reached",
                                           "Daily loss limit reached — trading disabled until tomorrow (UTC).", "critical")
        if st["consecutive_losses"] >= p["max_consecutive_losses"] and trade.realized_pnl < 0:
            await self.store.risk_event("losing_streak", "warning",
                                        f"{st['consecutive_losses']} consecutive losses — cooldown "
                                        f"{p['cooldown_after_streak_min']} min")
            await self.notifier.notify("risk", "Losing streak cooldown",
                                       f"{st['consecutive_losses']} losses in a row — entries paused for "
                                       f"{p['cooldown_after_streak_min']} min.", "warning")
        await self.store.set_setting("risk_alerts", flags)

    # ------------------------------------------------------------------ safety supervisor
    async def _supervisor_loop(self) -> None:
        sf = self.safety = await self.store.safety()
        age = self.market.data_age()
        if age > sf["stale_data_seconds"]:
            self.stale_since = self.stale_since or time.time()
            if self.guards.set("stale", f"market data stale ({age:.0f}s)"):
                await self.store.system_event("warning", "market", "stale", f"market data stale for {age:.0f}s")
                await self.notifier.notify("system", "Market data stale",
                                           f"No fresh BTC market data for {age:.0f}s — new entries paused.", "warning")
            if time.time() - self.stale_since > sf["stale_halt_minutes"] * 60:
                await self.halt(f"market data stale for more than {sf['stale_halt_minutes']} min")
        else:
            self.stale_since = None
            if self.guards.clear("stale"):
                await self.store.system_event("info", "market", "fresh", "market data restored")
                await self.notifier.notify("system", "Market data restored", "Fresh BTC market data again.")
        if self.clock_skew_ms is not None and abs(self.clock_skew_ms) > sf["max_clock_skew_ms"]:
            if self.guards.set("clock", f"clock skew {self.clock_skew_ms} ms vs Binance"):
                await self.store.system_event("warning", "clock", "skew", f"clock skew {self.clock_skew_ms} ms")
        else:
            self.guards.clear("clock")
        errs = self.public.errors.count(300) + len([t for t in self.market.errors if t > time.time() - 300])
        if self.live_adapter is not None:
            errs += self.live_adapter.errors.count(300)
        if errs >= sf["api_error_threshold"]:
            await self.halt(f"{errs} Binance API errors in 5 minutes")
        ms = self.market.status()
        await self.store.set_component(
            "market_ws", "ok" if ms["ws_connected"] else ("degraded" if age < sf["stale_data_seconds"] else "down"),
            "connected" if ms["ws_connected"] else "disconnected — REST fallback", ms)
        await self.store.set_component("database", "ok", "reachable")
        ai_ok = self.ai.provider.available
        await self.store.set_component(
            "openai", "disabled" if not ai_ok or self.params["ai_mode"] == "off" else
            ("ok" if self.ai.last_status in ("ok", "unknown") else "degraded"),
            self.ai.last_error or ("not configured" if not ai_ok else f"mode {self.params['ai_mode']}"))

    # ------------------------------------------------------------------ commands
    async def _command_loop(self) -> None:
        for c in await self.store.pending_commands():
            try:
                result = await self.execute_command(c)
                await self.store.finish_command(c.id, "done", result)
            except Exception as e:  # noqa: BLE001
                log.exception("command %s failed", c.kind)
                await self.store.finish_command(c.id, "failed", {"error": str(e)[:500]})

    async def execute_command(self, c: M.Command) -> dict[str, Any]:
        k, p = c.kind, c.payload or {}
        if k == "kill":
            return await self.kill(c.requested_by or "user", p.get("reason", "kill switch"))
        if k == "close_trade":
            t = await self.store.get_trade(int(p["trade_id"]))
            if t is None or t.status not in ("open", "closing"):
                return {"ok": False, "message": "trade not open"}
            leg = self.leg(t.mode)
            ok = await leg.execu.close_trade(t, "manual close") if leg else False
            if ok:
                t2 = await self.store.get_trade(t.id)
                await leg.monitor._notify_close(t2)  # noqa: SLF001
            return {"ok": ok}
        if k == "reconcile":
            for m in ("paper", "live"):
                if m in self.legs and (m == "paper" or self.mode == "live" or p.get("force")):
                    await self._reconcile(m)
            return {"ok": True}
        if k == "verify_credentials":
            await self._check_credentials()
            return {"ok": True, "status": self.cred.get("status")}
        if k == "analyze_now":
            return await self.analyze_now(p.get("trigger", "manual"))
        if k == "evaluate_now":
            ev = await self.evaluate_cycle()
            return {"ok": True, "decision": ev.decision if ev else None}
        if k == "paper_reset":
            if await self.store.open_trades("paper"):
                return {"ok": False, "message": "close open paper trades first"}
            self.paper.reset(float(p.get("balance", self.s.paper_starting_balance)))
            await self.store.set_setting("paper_state", self.paper.snapshot())
            return {"ok": True}
        if k == "protection_test":
            return await self.protection_test()
        return {"ok": False, "message": f"unknown command {k}"}

    async def kill(self, by: str, reason: str) -> dict[str, Any]:
        sf = await self.store.safety()
        self.trading = await self.store.update_trading(
            {"kill_switch": True, "strategy_enabled": False, "kill_engaged_at": utcnow().isoformat()}, by)
        actions = []
        for mode, leg in self.legs.items():
            has_trades = bool(await self.store.open_trades(mode))
            if mode == "live" and not (has_trades or self.mode == "live"):
                continue
            try:
                pos = await leg.adapter.get_position(self.s.symbol)
            except ExchangeError as e:
                actions.append(f"{mode}: position query failed ({e})")
                pos = None
            if sf["kill_positions"] == "close" and (pos is not None or has_trades):
                ok = await leg.execu.emergency_flatten(f"kill switch ({reason})")
                actions.append(f"{mode}: positions flattened={ok}")
            elif sf["kill_cancel_orders"]:
                try:
                    orders = await leg.adapter.get_open_orders(self.s.symbol)
                    prot_ids = set()
                    for t in await self.store.open_trades(mode):
                        prot_ids |= {v for k2, v in (t.protection or {}).items() if k2 in ("sl", "tp1", "tp2")}
                    for o in orders:
                        if leg.execu.is_ours(o.client_order_id) and o.client_order_id not in prot_ids:
                            await leg.execu._cancel_quiet(o.client_order_id, o.conditional)  # noqa: SLF001
                    actions.append(f"{mode}: non-protective orders cancelled, positions kept with protection")
                except ExchangeError as e:
                    actions.append(f"{mode}: order cancel failed ({e})")
        if self.mode == "live" and sf["kill_to_paper"]:
            self.trading = await self.store.update_trading({"mode": "paper"}, by)
            actions.append("mode switched to PAPER")
        await self.store.mark_readiness("kill_switch", True, "; ".join(actions)[:300])
        await self.store.system_event("warning", "safety", "kill_switch", f"KILL SWITCH by {by}: {reason}",
                                      {"actions": actions})
        await self.notifier.notify("risk", "KILL SWITCH engaged", "All trading stopped. " + "; ".join(actions),
                                   "critical", {"url": "/trading"})
        return {"ok": True, "actions": actions}

    async def protection_test(self) -> dict[str, Any]:
        """Place and cancel a far-away reduce-only stop on the live venue to prove the SL path works."""
        leg = self.legs.get("live")
        if leg is None:
            return {"ok": False, "message": "no live credentials configured"}
        ex = leg.adapter
        try:
            if await ex.get_position(self.s.symbol) is not None:
                return {"ok": False, "message": "run the test while flat"}
            mark = await ex.get_mark_price(self.s.symbol)
            filters = await ex.get_filters(self.s.symbol)
            cid = leg.execu.cid(0, "T", int(time.time()) % 100000)
            info = await ex.place_stop_market(self.s.symbol, "SELL", mark * 0.5, float(filters.min_qty), cid)
            found = any(o.client_order_id == cid for o in await ex.get_open_orders(self.s.symbol))
            cancelled = await ex.cancel_order(self.s.symbol, cid, True)
            gone = not any(o.client_order_id == cid for o in await ex.get_open_orders(self.s.symbol))
            ok = found and cancelled and gone
            await self.store.mark_readiness("protection_test", ok,
                                            f"placed={info.status} visible={found} cancelled={cancelled}")
            await self.store.system_event("info" if ok else "warning", "execution", "protection_test",
                                          f"live stop-loss self-test {'passed' if ok else 'FAILED'}")
            return {"ok": ok, "visible": found, "cancelled": cancelled}
        except ExchangeError as e:
            await self.store.mark_readiness("protection_test", False, str(e)[:300])
            return {"ok": False, "message": str(e)}

    async def analyze_now(self, trigger: str = "manual") -> dict[str, Any]:
        views = self._views()
        extras = self._extras()
        ev = self.strategy.evaluate(views, extras, self.params, StrategyState())
        leg = self.leg()
        acct = pos = None
        if leg:
            try:
                acct = await leg.adapter.get_account()
                pos = await leg.adapter.get_position(self.s.symbol)
            except ExchangeError:
                pass
        ctx = build_context(ev, views, extras, self.market.snapshot(), acct, pos,
                            await self.store.recent_closed_trades(self.mode, 5), None, self.mode)
        task = ("Review the open position: recommended_action HOLD, TIGHTEN_STOP or CLOSE; decision = the position's "
                "direction if the thesis still holds, else NO_TRADE." if pos else
                "Give an objective market read for BTCUSDT perpetual. decision=LONG/SHORT only if there is a clear, "
                "well-supported bias; otherwise NO_TRADE. recommended_action=WAIT.")
        res, aid, err = await self.ai.analyze("position_review" if pos else trigger, ctx, self.params, task)
        if res and pos and res.response.recommended_action == "CLOSE":
            await self.notifier.notify("ai", "AI suggests closing", f"AI review suggests closing BTC {pos.direction} "
                                       f"({res.response.confidence}): {res.response.reasoning[:140]}", "warning",
                                       {"url": "/ai"})
        return {"ok": res is not None, "analysis_id": aid, "error": err}

    async def _ai_loop(self) -> None:
        p = self.params
        if p["ai_mode"] == "off" or not self.ai.provider.available:
            return
        now = time.time()
        has_pos = bool(await self.store.open_trades(self.mode))
        if has_pos and p["ai_position_review_min"] and now - self.last_review_ai > p["ai_position_review_min"] * 60:
            self.last_review_ai = now
            await self.analyze_now("position_review")
        elif p["ai_periodic_min"] and now - self.last_periodic_ai > p["ai_periodic_min"] * 60:
            self.last_periodic_ai = now
            await self.analyze_now("periodic")

    # ------------------------------------------------------------------ periodic housekeeping
    async def _check_credentials(self) -> None:
        if self.live_adapter is None:
            self.cred = {"status": "missing"}
            await store_credential_status(self.store, await check_credentials(self.s, None))
            await self.store.set_component("binance", "ok", "public market data only (no API key) — paper mode")
            return
        res = await check_credentials(self.s, self.live_adapter)
        await store_credential_status(self.store, res)
        self.cred = await credential_status(self.store, self.s)
        st = res["status"]
        await self.store.set_component("binance", "ok" if st == "connected" else "degraded",
                                       f"credentials {st}" + (": " + "; ".join(res["problems"]) if res["problems"] else ""))
        if self.mode == "live" and st != "connected":
            await self.halt(f"live credentials {st}")

    async def _slow_loop(self) -> None:
        now = time.time()
        # clock skew
        try:
            t0 = time.time()
            server = await self.public.server_time()
            t1 = time.time()
            self.clock_skew_ms = int((t0 + t1) / 2 * 1000) - server
        except ExchangeError:
            pass
        # equity snapshots every 5 min
        slot = int(now // 300)
        if getattr(self, "_equity_slot", None) != slot:
            self._equity_slot = slot
            for mode, leg in self.legs.items():
                if mode == "live" and self.cred.get("status") not in ("connected", "unsafe"):
                    continue
                try:
                    a = await leg.adapter.get_account()
                    await self.store.record_equity(mode, a.equity, a.wallet_balance, a.unrealized_pnl)
                except ExchangeError:
                    pass
        if int(now // 600) != getattr(self, "_cred_slot", None):
            self._cred_slot = int(now // 600)
            await self._check_credentials()
        if int(now // 86400) != getattr(self, "_prune_slot", None):
            self._prune_slot = int(now // 86400)
            await self.store.prune(utcnow())

    async def _publish_loop(self) -> None:
        snap = self.market.snapshot()
        snap["forming_5m"] = self.market.forming.get("5m")
        snap["forming_1m"] = self.market.forming.get("1m")
        await self.store.publish("ticker", snap)
        acct_view = {}
        for mode, leg in self.legs.items():
            if mode == "live" and self.cred.get("status") not in ("connected", "unsafe"):
                continue
            if mode == "live" and not (self.mode == "live" or int(time.time()) % 15 == 0):
                continue
            try:
                a = await leg.adapter.get_account()
                acct_view[mode] = {"equity": a.equity, "wallet": a.wallet_balance, "available": a.available_balance,
                                   "unrealized": a.unrealized_pnl}
            except ExchangeError:
                pass
        if acct_view:
            prev = (await self.store.live_state(["accounts"])).get("accounts", {})
            prev.pop("_updated_at", None)
            await self.store.publish("accounts", {**prev, **acct_view})
        await self.store.publish("engine", {
            "mode": self.mode, "guards": list(self.guards.reasons.values()), "halted": self.trading.get("halted"),
            "halt_reason": self.trading.get("halt_reason"), "kill_switch": self.trading.get("kill_switch"),
            "strategy_enabled": self.trading.get("strategy_enabled"), "clock_skew_ms": self.clock_skew_ms,
            "market": self.market.status(), "version": __version__, "uptime_s": int(time.time() - self.started_at),
            "live_reconciled": self.legs["live"].reconciled if "live" in self.legs else None,
            "credential_status": self.cred.get("status"),
        })

    async def _heartbeat_loop(self) -> None:
        Path(self.s.engine_heartbeat_file).write_text(str(int(time.time())))
        await self.store.set_component("engine", "ok", f"running v{__version__}",
                                       {"uptime_s": int(time.time() - self.started_at), "mode": self.mode})
        if self.trading.get("halted"):
            await self.store.set_component("trading", "degraded", f"HALTED: {self.trading.get('halt_reason')}")
        elif self.trading.get("kill_switch"):
            await self.store.set_component("trading", "degraded", "kill switch engaged")
        elif self.guards.reasons:
            await self.store.set_component("trading", "degraded", "paused: " + "; ".join(self.guards.reasons.values()))
        else:
            await self.store.set_component("trading", "ok", f"{self.mode.upper()} · strategy "
                                           f"{'enabled' if self.trading.get('strategy_enabled') else 'disabled'}")
        await self.store.set_component("notifications", "ok" if self.notifier.sender.configured else "degraded",
                                       "web push configured" if self.notifier.sender.configured else "VAPID keys missing")

