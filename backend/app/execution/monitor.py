"""Position monitoring and exchange ↔ database reconciliation.

Both run against the ExecutionAdapter, so paper and live share them verbatim.
The venue is always treated as the source of truth; the database is corrected
to match it, and nothing is ever re-submitted without first checking what the
venue already holds.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.store import Store
from app.db import models as M
from app.db.types import utcnow
from app.exchange.models import ExchangeError, OrderInfo, PositionInfo
from app.execution.engine import ExecutionEngine, Notifier, fmt_usd

log = logging.getLogger("kestrel.monitor")

TradeClosedHook = Callable[[M.Trade], Awaitable[None]]


class PositionMonitor:
    def __init__(self, execu: ExecutionEngine, store: Store, notifier: Notifier,
                 halt: Callable[[str], Awaitable[None]], on_closed: TradeClosedHook | None = None) -> None:
        self.x = execu
        self.store = store
        self.notifier = notifier
        self.halt = halt
        self.on_closed = on_closed
        self.last_position: PositionInfo | None = None
        self.consecutive_failures = 0

    @property
    def ex(self):  # noqa: ANN201
        return self.x.ex

    async def run_once(self, safety: dict[str, Any]) -> dict[str, Any]:
        trades = [t for t in await self.store.open_trades(self.x.mode) if t.status == "open"]
        pos = await self.ex.get_position(self.x.symbol)
        orders = await self.ex.get_open_orders(self.x.symbol)
        self.last_position = pos
        self.consecutive_failures = 0
        await self._publish_position(pos, trades[0] if trades else None)
        result: dict[str, Any] = {"trades": len(trades), "position": pos.direction if pos else None}
        if not trades:
            return result
        trade = trades[0]
        if pos is None:
            await self.settle_closed(trade, orders)
            result["closed"] = trade.id
            return result
        if pos.direction != trade.direction:
            await self.halt(f"venue position {pos.direction} contradicts trade #{trade.id} {trade.direction}")
            await self.notifier.notify("risk", "Unexpected position", f"Venue shows {pos.direction}, Kestrel expected "
                                       f"{trade.direction}. Trading halted.", "critical")
            return result
        await self._handle_partial(trade, pos)
        trade = await self.store.get_trade(trade.id)
        status = await self.x.ensure_protection(trade, pos, await self.ex.get_open_orders(self.x.symbol))
        result["protection"] = status
        if status == "failed":
            await self.store.system_event("critical", "monitor", "protection_failed",
                                          f"trade #{trade.id}: stop-loss missing and could not be re-placed")
            flat = await self.x.close_trade(trade, "emergency: stop-loss could not be restored")
            await self.halt("stop-loss could not be verified on an open position")
            await self.notifier.notify("risk", "EMERGENCY: stop-loss lost",
                                       f"Stop-loss for BTC {trade.direction} could not be restored. Position "
                                       f"{'closed' if flat else 'NOT closed — act manually now'}. Trading halted.",
                                       "critical", {"trade_id": trade.id})
        elif status == "repaired":
            await self.store.risk_event("protection_repaired", "warning",
                                        f"trade #{trade.id}: missing protective order re-placed")
            await self.notifier.notify("system", "Protective order restored",
                                       f"A missing stop/take-profit for BTC {trade.direction} was re-placed.", "warning")
        if (trade.protection or {}).get("software_tp"):
            await self._software_tp(trade, pos)
        return result

    async def _publish_position(self, pos: PositionInfo | None, trade: M.Trade | None) -> None:
        await self.store.upsert_position(
            self.x.mode, self.x.symbol,
            direction=pos.direction if pos else None, quantity=pos.size if pos else 0.0,
            entry_price=pos.entry_price if pos else None, mark_price=pos.mark_price if pos else None,
            unrealized_pnl=pos.unrealized_pnl if pos else 0.0, leverage=pos.leverage if pos else None,
            liquidation_price=pos.liquidation_price if pos else None, margin=pos.margin if pos else None,
            trade_id=trade.id if trade else None)

    async def _handle_partial(self, trade: M.Trade, pos: PositionInfo) -> None:
        """Detect TP1 fill (position reduced) and move the stop to breakeven if configured."""
        if trade.tp1_filled or not trade.tp1_price:
            return
        if pos.size >= trade.remaining_qty - 1e-9:
            return
        await self.store.update_trade(trade.id, tp1_filled=True, remaining_qty=pos.size)
        await self.store.trade_event(trade.id, "tp1_filled", f"TP1 filled — {pos.size} BTC remaining")
        await self.notifier.notify("take_profit", f"BTC {trade.direction} TP1 hit",
                                   f"BTC {trade.direction} TP1 hit at {fmt_usd(trade.tp1_price)} — "
                                   f"{pos.size} BTC still open", "info", {"trade_id": trade.id})
        trade = await self.store.get_trade(trade.id)
        if (trade.protection or {}).get("breakeven_after_tp1", True) and trade.entry_price:
            d = 1 if trade.direction == "LONG" else -1
            be = trade.entry_price * (1 + d * 0.0012)  # covers round-trip taker fees
            if d * (pos.mark_price - be) > trade.entry_price * 0.0005:
                await self.x.move_stop(trade, be, pos.size, "breakeven after TP1")
        else:
            # keep stop, but its quantity must match the remaining size (reduce-only excess is harmless)
            pass

    async def _software_tp(self, trade: M.Trade, pos: PositionInfo) -> None:
        d = 1 if trade.direction == "LONG" else -1
        target = trade.tp2_price if trade.tp1_filled or not trade.tp1_price else trade.tp1_price
        if target and d * (pos.mark_price - target) >= 0:
            await self.x.close_trade(trade, "take_profit (software)")

    async def settle_closed(self, trade: M.Trade, open_orders: list[OrderInfo] | None = None) -> M.Trade:
        """Position is flat on the venue: work out why, settle P&L, clean up leftovers, notify."""
        prot = trade.protection or {}
        reason = "external"
        for kind in ("sl", "tp2", "tp1"):
            cid = prot.get(kind)
            if not cid:
                continue
            try:
                info = await self.ex.get_order(self.x.symbol, cid, True)
            except ExchangeError:
                info = None
            if info is not None and info.status in ("FILLED", "TRIGGERED", "FINISHED"):
                if kind == "sl":
                    moved = trade.initial_stop and trade.stop_price and abs(trade.stop_price - trade.initial_stop) > 1e-6
                    reason = "breakeven_stop" if moved else "stop_loss"
                else:
                    reason = "take_profit"
                break
        if reason == "external" and trade.liquidation_price:
            try:
                mark = await self.ex.get_mark_price(self.x.symbol)
                d = 1 if trade.direction == "LONG" else -1
                if d * (mark - trade.liquidation_price) <= mark * 0.002:
                    reason = "liquidation"
            except ExchangeError:
                pass
        try:
            await self.ex.cancel_all_orders(self.x.symbol)
        except ExchangeError as e:
            log.warning("cleanup cancel failed: %s", e)
        await self.x.sync_trade_orders(trade.id)
        t = await self.x.finalize(trade, reason)
        await self._notify_close(t)
        if reason in ("liquidation", "external"):
            await self.store.risk_event(f"closed_{reason}", "critical" if reason == "liquidation" else "warning",
                                        f"trade #{t.id} closed by {reason}")
            if reason == "liquidation":
                await self.halt("position was liquidated")
        if self.on_closed:
            await self.on_closed(t)
        return t

    async def _notify_close(self, t: M.Trade) -> None:
        px = fmt_usd(t.exit_price) if t.exit_price else "market"
        pnl = f"{t.realized_pnl:+,.2f} USDT"
        if t.exit_reason in ("stop_loss", "breakeven_stop"):
            await self.notifier.notify("stop_loss", f"BTC {t.direction} stopped",
                                       f"BTC {t.direction} stopped at {px} · {pnl}", "warning", {"trade_id": t.id})
        elif t.exit_reason == "take_profit":
            await self.notifier.notify("take_profit", f"BTC {t.direction} TP2 hit",
                                       f"BTC {t.direction} take-profit hit at {px} · {pnl}", "info", {"trade_id": t.id})
        else:
            await self.notifier.notify("trade_closed", f"BTC {t.direction} closed",
                                       f"BTC {t.direction} closed ({t.exit_reason}) at {px} · {pnl}",
                                       "critical" if t.exit_reason == "liquidation" else "info", {"trade_id": t.id})


class Reconciler:
    def __init__(self, execu: ExecutionEngine, monitor: PositionMonitor, store: Store, notifier: Notifier,
                 halt: Callable[[str], Awaitable[None]]) -> None:
        self.x = execu
        self.monitor = monitor
        self.store = store
        self.notifier = notifier
        self.halt = halt

    @property
    def ex(self):  # noqa: ANN201
        return self.x.ex

    async def run(self, safety: dict[str, Any]) -> dict[str, Any]:
        sym = self.x.symbol
        report: dict[str, Any] = {"mode": self.x.mode, "actions": []}
        act = report["actions"].append
        async with self.x.lock:
            pos = await self.ex.get_position(sym)
            orders = await self.ex.get_open_orders(sym)
        trades = await self.store.open_trades(self.x.mode)
        report["position"] = pos.direction if pos else None
        report["open_orders"] = len(orders)
        report["db_trades"] = [t.id for t in trades]

        for t in trades:
            if t.status == "pending":
                await self._resolve_pending(t, pos, act)
            elif t.status == "closing":
                if pos is None:
                    await self.monitor.settle_closed(t, orders)
                    act(f"trade #{t.id}: flatten completed while away — settled")
                else:
                    await self.store.update_trade(t.id, status="open")
                    act(f"trade #{t.id}: interrupted close — position still open, resuming monitoring")

        trades = await self.store.open_trades(self.x.mode)
        async with self.x.lock:
            pos = await self.ex.get_position(sym)
            orders = await self.ex.get_open_orders(sym)
        open_trades = [t for t in trades if t.status == "open"]

        if open_trades:
            t = open_trades[0]
            if pos is None:
                await self.monitor.settle_closed(t, orders)
                act(f"trade #{t.id}: closed on venue while Kestrel was not watching — settled")
            elif pos.direction != t.direction:
                await self.halt(f"reconciliation: venue {pos.direction} vs trade #{t.id} {t.direction}")
                act(f"trade #{t.id}: direction mismatch — halted")
            else:
                if abs(pos.size - (t.remaining_qty or t.quantity)) > 1e-9 and not t.tp1_filled and t.tp1_price \
                        and pos.size < t.remaining_qty:
                    await self.monitor._handle_partial(t, pos)  # noqa: SLF001
                    t = await self.store.get_trade(t.id)
                status = await self.x.ensure_protection(t, pos, orders)
                act(f"trade #{t.id}: position matches, protection {status}")
                if status == "failed":
                    await self.x.close_trade(t, "emergency: stop-loss could not be restored")
                    await self.halt("reconciliation could not restore stop-loss")
                    act(f"trade #{t.id}: EMERGENCY flatten")
        elif pos is not None:
            await self._unexpected_position(pos, orders, safety, act)
        else:
            stale = [o for o in orders if self.x.is_ours(o.client_order_id)]
            for o in stale:
                async with self.x.lock:
                    await self.x._cancel_quiet(o.client_order_id, o.conditional)  # noqa: SLF001
                act(f"cancelled orphan order {o.client_order_id} ({o.type})")
            foreign = [o for o in orders if not self.x.is_ours(o.client_order_id)]
            if foreign:
                act(f"{len(foreign)} open order(s) not created by Kestrel left untouched")
                report["foreign_orders"] = len(foreign)
        report["ok"] = True
        report["at"] = utcnow().isoformat()
        return report

    async def _resolve_pending(self, t: M.Trade, pos: PositionInfo | None, act: Callable[[str], None]) -> None:
        cid = self.x.cid(t.id, "E")
        try:
            info = await self.ex.get_order(self.x.symbol, cid, False)
        except ExchangeError:
            info = None
        if info is not None and info.is_open:
            try:
                await self.ex.cancel_order(self.x.symbol, cid, False)
            except ExchangeError:
                pass
            info = await self.ex.get_order(self.x.symbol, cid, False)
        filled = info is not None and info.filled_qty > 0
        if filled and pos is not None and pos.direction == t.direction:
            await self.store.update_trade(t.id, status="open", entry_price=pos.entry_price, quantity=pos.size,
                                          remaining_qty=pos.size, opened_at=t.opened_at or utcnow(),
                                          liquidation_price=pos.liquidation_price)
            await self.store.trade_event(t.id, "recovered", "entry filled before restart — resuming and protecting")
            act(f"trade #{t.id}: pending entry had filled — protection will be verified")
        else:
            await self.store.update_trade(t.id, status="cancelled", closed_at=utcnow(), exit_reason="entry_unresolved")
            await self.store.trade_event(t.id, "cancelled", "pending entry never filled (resolved on restart)")
            act(f"trade #{t.id}: pending entry did not fill — cancelled")

    async def _unexpected_position(self, pos: PositionInfo, orders: list[OrderInfo], safety: dict[str, Any],
                                   act: Callable[[str], None]) -> None:
        policy = safety.get("unexpected_position_policy", "protect")
        msg = f"venue holds an unknown {pos.direction} {pos.size} BTC position @ {pos.entry_price:,.1f}"
        await self.store.system_event("critical", "reconcile", "unexpected_position", msg,
                                      {"policy": policy, "size": pos.size})
        await self.halt(f"unexpected position detected ({policy})")
        if policy == "flatten":
            ok = await self.x.emergency_flatten("unexpected position (policy flatten)")
            act(f"unexpected position — flattened: {ok}")
            await self.notifier.notify("risk", "Unknown position closed", msg + f". Flattened: {ok}. Trading halted.",
                                       "critical")
            return
        if policy == "halt_only":
            act("unexpected position — halted only (policy)")
            await self.notifier.notify("risk", "Unknown position detected", msg + ". Trading halted; no action taken.",
                                       "critical")
            return
        # protect: adopt it into the journal and make sure a stop exists
        d = 1 if pos.direction == "LONG" else -1
        stops = [o for o in orders if o.is_open and o.type in ("STOP_MARKET", "STOP")
                 and o.side == ("SELL" if d == 1 else "BUY")]
        pct = float(safety.get("emergency_stop_pct", 1.5)) / 100
        stop = stops[0].trigger_price if stops and stops[0].trigger_price else pos.mark_price * (1 - d * pct)
        t = await self.store.create_trade(
            mode=self.x.mode, symbol=self.x.symbol, direction=pos.direction, status="open", origin="recovered",
            quantity=pos.size, remaining_qty=pos.size, leverage=pos.leverage or 1, entry_price=pos.entry_price,
            stop_price=stop, initial_stop=stop, tp2_price=None, opened_at=utcnow(),
            liquidation_price=pos.liquidation_price, reason="adopted unknown position (reconciliation)",
            protection={"seq": 0, "software_tp": True, "breakeven_after_tp1": False,
                        **({"sl": stops[0].client_order_id} if stops and self.x.is_ours(stops[0].client_order_id) else {})})
        await self.store.trade_event(t.id, "adopted", msg)
        async with self.x.lock:
            ok = await self.x._place_protective(t, "sl", stop, pos.size) if not stops else True  # noqa: SLF001
        act(f"unexpected position adopted as trade #{t.id}; stop {'present' if stops else ('placed' if ok else 'FAILED')}")
        if not ok:
            await self.x.close_trade(t, "emergency: could not protect unknown position")
        await self.notifier.notify("risk", "Unknown position protected",
                                   msg + f". Adopted as trade #{t.id} with stop {fmt_usd(stop)}. Trading halted — "
                                         "review and re-enable manually.", "critical")
