"""Execution engine — identical code path for paper and live; only the adapter differs.

Safety rules enforced here:
* Every entry is preceded by fresh account / margin / leverage / price / slippage /
  stop / take-profit / duplicate checks against the *venue*, not cached state.
* Client order ids are deterministic per trade, kind and attempt (plus a per-install
  salt), so a retry after an ambiguous failure can be resolved by querying, never
  by blindly re-submitting.
* A filled entry must get a verified exchange-side stop-loss. If the stop cannot be
  placed AND verified, the position is flattened immediately, trading is halted and
  a critical alert is sent. A position is never knowingly left unprotected.
* Protective orders are replaced place-new-then-cancel-old, so a stop move never
  opens an unprotected window.
* Flattening closes first and cancels protective orders only once flat; if the
  position cannot be closed, existing protection is left in place.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.core.store import Store
from app.db import models as M
from app.db.types import utcnow
from app.exchange.base import ExecutionAdapter
from app.exchange.models import ExchangeError, OrderInfo, OrderRejected, PositionInfo, UnknownOrderStatus, side_for

log = logging.getLogger("kestrel.execution")

PROTECTIVE_KINDS = ("sl", "tp1", "tp2")


class Notifier(Protocol):
    async def notify(self, category: str, title: str, body: str, severity: str = "info",
                     data: dict[str, Any] | None = None) -> None: ...


class NullNotifier:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []

    async def notify(self, category: str, title: str, body: str, severity: str = "info",
                     data: dict[str, Any] | None = None) -> None:
        self.sent.append((category, title, body, severity))


@dataclass
class TradePlan:
    signal_id: int | None
    direction: str
    entry: float  # signal reference price
    stop: float
    tp1: float
    tp2: float
    quantity: float
    tp1_qty: float
    tp2_qty: float
    split: bool
    leverage: int
    risk_usdt: float
    equity: float
    max_slippage_bps: float
    min_rr: float
    tp_failure_policy: str = "close"
    strategy_config_id: int | None = None
    strategy_version: int | None = None
    confidence: float | None = None
    ai_decision: str | None = None
    ai_confidence: float | None = None
    market_regime: str | None = None
    reasons: list[str] = field(default_factory=list)
    snapshot: dict[str, Any] = field(default_factory=dict)
    origin: str = "strategy"
    breakeven_after_tp1: bool = True
    # Audit steps that happened before the trade row existed: (step, message, data)
    pre_events: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)


@dataclass
class ExecResult:
    ok: bool
    trade_id: int | None
    message: str
    emergency: bool = False


def fmt_usd(x: float) -> str:
    return f"${x:,.0f}" if abs(x) >= 1000 else f"${x:,.2f}"


class ExecutionEngine:
    def __init__(self, adapter: ExecutionAdapter, store: Store, notifier: Notifier, symbol: str,
                 halt: Callable[[str], Awaitable[None]], salt: str = "x0",
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 verify_attempts: int = 5) -> None:
        self.ex = adapter
        self.store = store
        self.notifier = notifier
        self.symbol = symbol
        self.halt = halt
        self.salt = salt
        self._sleep = sleep
        self.verify_attempts = verify_attempts
        self.lock = asyncio.Lock()  # one execution action at a time per adapter

    @property
    def mode(self) -> str:
        return self.ex.mode

    def cid(self, trade_id: int, kind: str, n: int = 0) -> str:
        return f"kst{self.salt}-{trade_id}-{kind}{n}"

    def is_ours(self, client_order_id: str) -> bool:
        return client_order_id.startswith(f"kst{self.salt}-")

    # ------------------------------------------------------------------ helpers
    async def _event(self, trade_id: int, step: str, message: str, **data: Any) -> None:
        try:
            await self.store.trade_event(trade_id, step, message, data)
        except Exception:  # noqa: BLE001
            log.exception("failed to write trade event")

    async def _record_order(self, trade_id: int | None, kind: str, cid: str, side: str, otype: str, qty: float,
                            trigger: float | None = None, reduce_only: bool = False, conditional: bool = False) -> None:
        await self.store.create_order(trade_id=trade_id, mode=self.mode, symbol=self.symbol, kind=kind,
                                      client_order_id=cid, side=side, type=otype, quantity=qty,
                                      trigger_price=trigger, reduce_only=reduce_only, conditional=conditional,
                                      status="SUBMITTING")

    async def _sync_order(self, info: OrderInfo | None, cid: str, error: str | None = None) -> None:
        if info is None:
            await self.store.update_order(cid, status="NOT_FOUND" if error is None else "FAILED", error=error)
            return
        await self.store.update_order(cid, status=info.status, venue_order_id=info.venue_order_id,
                                      filled_qty=info.filled_qty, avg_price=info.avg_price, raw=_slim(info.raw),
                                      error=error)

    async def _resolve_unknown(self, cid: str, conditional: bool) -> OrderInfo | None:
        """After an ambiguous submission, poll the venue for the order by client id."""
        delay = 0.5
        for _ in range(self.verify_attempts):
            await self._sleep(delay)
            try:
                info = await self.ex.get_order(self.symbol, cid, conditional)
                if info is not None:
                    return info
            except ExchangeError as e:
                log.warning("order lookup failed while resolving %s: %s", cid, e)
            delay = min(delay * 2, 4.0)
        return None

    async def _position(self) -> PositionInfo | None:
        return await self.ex.get_position(self.symbol)

    # ------------------------------------------------------------------ entry
    async def open_trade(self, plan: TradePlan) -> ExecResult:
        async with self.lock:
            return await self._open_trade(plan)

    async def _open_trade(self, plan: TradePlan) -> ExecResult:
        d = 1 if plan.direction == "LONG" else -1
        # --- 1-10: pre-submission validation against the venue -------------------------
        problems: list[str] = []
        if plan.direction not in ("LONG", "SHORT"):
            problems.append("invalid direction")
        if not (d * (plan.entry - plan.stop) > 0):
            problems.append("stop-loss on wrong side")
        if not (d * (plan.tp1 - plan.entry) > 0 and d * (plan.tp2 - plan.tp1) >= 0):
            problems.append("take-profit plan invalid")
        if plan.quantity <= 0:
            problems.append("zero quantity")
        if problems:
            return ExecResult(False, None, "pre-check failed: " + ", ".join(problems))
        try:
            existing = await self.store.open_trades(self.mode)
            if existing:
                return ExecResult(False, None, f"duplicate prevented: trade #{existing[0].id} already open")
            pos = await self._position()
            if pos is not None:
                return ExecResult(False, None, f"duplicate prevented: venue already holds a {pos.direction} position")
            open_orders = await self.ex.get_open_orders(self.symbol)
            if any(self.is_ours(o.client_order_id) for o in open_orders):
                return ExecResult(False, None, "stale Kestrel orders on venue — reconciliation must run first")
            account = await self.ex.get_account()
            filters = await self.ex.get_filters(self.symbol)
            margin_needed = plan.quantity * plan.entry / plan.leverage
            if account.available_balance < margin_needed * 1.02:
                return ExecResult(False, None, f"insufficient margin: need {margin_needed:,.2f}, "
                                               f"available {account.available_balance:,.2f}")
            await self.ex.ensure_isolated_margin(self.symbol)
            lev = await self.ex.set_leverage(self.symbol, plan.leverage)
            if lev != plan.leverage:
                return ExecResult(False, None, f"venue confirmed leverage {lev}×, expected {plan.leverage}×")
            mark = await self.ex.get_mark_price(self.symbol)
            book = await self.ex.get_book_ticker(self.symbol)
            ref = (book.ask if d == 1 else book.bid) if book and book.bid > 0 else mark
        except ExchangeError as e:
            return ExecResult(False, None, f"pre-check exchange error: {e}")

        slip_bps = abs(ref - plan.entry) / plan.entry * 10_000
        if slip_bps > plan.max_slippage_bps:
            return ExecResult(False, None, f"price moved {slip_bps:.1f} bps from signal (max {plan.max_slippage_bps})")
        if not d * (ref - plan.stop) > 0 or not d * (mark - plan.stop) > 0:
            return ExecResult(False, None, "current price already beyond the stop-loss")
        if not d * (plan.tp1 - ref) > 0:
            return ExecResult(False, None, "current price already beyond TP1")
        risk_now = d * (ref - plan.stop)
        rr_now = ((plan.tp1_qty * d * (plan.tp1 - ref) + plan.tp2_qty * d * (plan.tp2 - ref)) / plan.quantity) / risk_now
        if rr_now < plan.min_rr * 0.95:
            return ExecResult(False, None, f"reward:risk at current price {rr_now:.2f} below minimum")
        if plan.quantity * risk_now > plan.risk_usdt * 1.15:
            return ExecResult(False, None, "risk at current price exceeds budget")

        # --- create trade -----------------------------------------------------------
        trade = await self.store.create_trade(
            mode=self.mode, symbol=self.symbol, direction=plan.direction, status="pending", signal_id=plan.signal_id,
            strategy_config_id=plan.strategy_config_id, strategy_version=plan.strategy_version, origin=plan.origin,
            quantity=plan.quantity, remaining_qty=0.0, leverage=plan.leverage, stop_price=plan.stop,
            initial_stop=plan.stop, tp1_price=plan.tp1 if plan.split else None, tp2_price=plan.tp2,
            risk_usdt=plan.risk_usdt, equity_at_entry=plan.equity, confidence=plan.confidence,
            ai_decision=plan.ai_decision, ai_confidence=plan.ai_confidence, market_regime=plan.market_regime,
            reason="; ".join(plan.reasons), reasons=plan.reasons, snapshot=plan.snapshot,
            protection={"seq": 0, "breakeven_after_tp1": plan.breakeven_after_tp1, "split": plan.split})
        tid = trade.id
        for step, message, data in plan.pre_events:
            await self._event(tid, step, message, **data)
        await self._event(tid, "validated", f"pre-trade checks passed (price {ref:,.1f}, slippage {slip_bps:.1f} bps)",
                          mark=mark, ref=ref, slippage_bps=slip_bps, leverage=lev,
                          available=account.available_balance)

        # --- entry order --------------------------------------------------------------
        entry_side = side_for(plan.direction)
        cid = self.cid(tid, "E")
        await self._record_order(tid, "entry", cid, entry_side, "MARKET", plan.quantity)
        await self._event(tid, "order_submitted", f"{entry_side} {plan.quantity} {self.symbol} MARKET", client_id=cid)
        info: OrderInfo | None
        try:
            info = await self.ex.place_market_order(self.symbol, entry_side, plan.quantity, cid)
        except UnknownOrderStatus as e:
            await self._event(tid, "entry_unknown", f"entry status unknown ({e}); resolving by query")
            info = await self._resolve_unknown(cid, conditional=False)
            if info is None:
                pos = await self._safe_position()
                if pos is not None and pos.direction == plan.direction:
                    await self._event(tid, "entry_inferred", "order not queryable but a matching position exists")
                    info = OrderInfo(self.symbol, cid, "", entry_side, "MARKET", "FILLED", pos.size, pos.size,
                                     pos.entry_price)
                else:
                    await self._sync_order(None, cid, "not found after unknown status")
                    await self.store.update_trade(tid, status="cancelled", closed_at=utcnow(), exit_reason="entry_failed")
                    await self._event(tid, "cancelled", "entry never reached the venue")
                    return ExecResult(False, tid, "entry status unknown and no order/position found — no trade")
        except ExchangeError as e:
            await self._sync_order(None, cid, str(e))
            await self.store.update_trade(tid, status="cancelled", closed_at=utcnow(), exit_reason="entry_rejected")
            await self._event(tid, "cancelled", f"entry rejected: {e}")
            return ExecResult(False, tid, f"entry rejected: {e}")

        # Market orders should fill immediately; wait briefly if not reported yet.
        if not info.is_filled:
            for _ in range(self.verify_attempts):
                await self._sleep(0.5)
                q = await self.ex.get_order(self.symbol, cid, False)
                if q is not None:
                    info = q
                if info.is_filled or info.status in ("CANCELED", "EXPIRED", "REJECTED"):
                    break
            if info.is_open:
                try:
                    await self.ex.cancel_order(self.symbol, cid, False)
                except ExchangeError:
                    pass
                q = await self.ex.get_order(self.symbol, cid, False)
                info = q or info
        await self._sync_order(info, cid)
        filled = info.filled_qty
        pos = await self._safe_position()
        if pos is not None and pos.direction == plan.direction:
            filled = pos.size  # venue position is the truth
        if filled <= 0:
            await self.store.update_trade(tid, status="cancelled", closed_at=utcnow(), exit_reason="entry_not_filled")
            await self._event(tid, "cancelled", f"entry not filled (status {info.status})")
            return ExecResult(False, tid, "entry not filled")
        entry_px = (pos.entry_price if pos else None) or info.avg_price or ref
        tp1_q, tp2_q, split = plan.tp1_qty, plan.tp2_qty, plan.split
        if abs(filled - plan.quantity) > 1e-9:  # partial fill → re-split
            filters = await self.ex.get_filters(self.symbol)
            tp1_q = float(filters.round_qty_down(filled * (plan.tp1_qty / plan.quantity))) if split else 0.0
            tp2_q = float(filters.round_qty_down(filled - tp1_q))
            split = split and tp1_q >= filters.min_qty_for() and tp2_q >= filters.min_qty_for()
            if not split:
                tp1_q, tp2_q = 0.0, filled
        now = utcnow()
        trade = await self.store.update_trade(
            tid, status="open", entry_price=entry_px, quantity=filled, remaining_qty=filled, opened_at=now,
            liquidation_price=pos.liquidation_price if pos else None)
        await self._event(tid, "order_filled", f"entry filled {filled} @ {entry_px:,.2f}", avg_price=entry_px,
                          filled=filled, slippage_bps=abs(entry_px - plan.entry) / plan.entry * 10_000)

        # --- protective stop (mandatory) -------------------------------------------------
        sl_ok = await self._place_protective(trade, "sl", plan.stop, filled)
        if not sl_ok:
            await self._event(tid, "emergency", "stop-loss could not be placed and verified — flattening")
            flat = await self._flatten(trade, "emergency: stop-loss not verified", emergency=True)
            await self.halt("stop-loss could not be placed/verified on a filled position")
            await self.notifier.notify("risk", "EMERGENCY: position flattened",
                                       f"BTC {plan.direction} could not be protected with a stop-loss and was "
                                       f"{'closed' if flat else 'NOT closed — manual intervention required'}. "
                                       "Trading halted.", "critical", {"trade_id": tid})
            return ExecResult(False, tid, "stop-loss failed — position flattened, trading halted", emergency=True)

        # --- take-profit(s) ---------------------------------------------------------
        trade = await self.store.get_trade(tid)
        tp_ok = True
        if split:
            tp_ok = await self._place_protective(trade, "tp1", plan.tp1, tp1_q)
            trade = await self.store.get_trade(tid)
        tp_ok = tp_ok and await self._place_protective(trade, "tp2", plan.tp2, tp2_q)
        trade = await self.store.get_trade(tid)
        if not tp_ok:
            if plan.tp_failure_policy == "close":
                await self._event(tid, "tp_failed", "take-profit could not be verified — closing per policy")
                await self._flatten(trade, "take-profit not verified", emergency=False)
                await self.store.risk_event("tp_failure", "warning", f"trade #{tid}: take-profit failed, closed")
                await self.notifier.notify("risk", "Trade closed: TP failed",
                                           f"BTC {plan.direction} closed — take-profit could not be placed.",
                                           "warning", {"trade_id": tid})
                return ExecResult(False, tid, "take-profit failed — position closed per policy")
            prot = dict(trade.protection or {})
            prot["software_tp"] = True
            await self.store.update_trade(tid, protection=prot)
            await self._event(tid, "tp_software", "take-profit managed in software (policy keep)")

        await self._event(tid, "protected", "stop-loss and take-profit verified on venue")
        await self.notifier.notify("trade_opened", f"BTC {plan.direction} opened",
                                   f"BTC {plan.direction} opened at {fmt_usd(entry_px)} · stop {fmt_usd(plan.stop)}"
                                   f" · {self.mode.upper()}", "info", {"trade_id": tid, "url": f"/history?trade={tid}"})
        return ExecResult(True, tid, f"{plan.direction} opened at {entry_px:,.2f}")

    async def _safe_position(self) -> PositionInfo | None:
        try:
            return await self._position()
        except ExchangeError:
            return None

    # ------------------------------------------------------------------ protective orders
    async def _place_protective(self, trade: M.Trade, kind: str, trigger: float, qty: float,
                                attempts: int = 3) -> bool:
        """Place + verify a reduce-only protective order; updates ``trade.protection``. Returns success."""
        side = side_for(trade.direction, closing=True)
        otype = "STOP_MARKET" if kind == "sl" else "TAKE_PROFIT_MARKET"
        prot = dict(trade.protection or {})
        for attempt in range(attempts):
            seq = int(prot.get("seq", 0)) + 1
            prot["seq"] = seq
            cid = self.cid(trade.id, kind.upper(), seq)
            await self._record_order(trade.id, kind, cid, side, otype, qty, trigger, reduce_only=True, conditional=True)
            info: OrderInfo | None = None
            try:
                if kind == "sl":
                    info = await self.ex.place_stop_market(self.symbol, side, trigger, qty, cid)
                else:
                    info = await self.ex.place_take_profit_market(self.symbol, side, trigger, qty, cid)
            except UnknownOrderStatus:
                info = await self._resolve_unknown(cid, conditional=True)
            except OrderRejected as e:
                await self._sync_order(None, cid, str(e))
                await self._event(trade.id, f"{kind}_rejected", f"{otype} rejected: {e}", attempt=attempt)
                if e.code == -2021:  # would immediately trigger: price is already through the level
                    break
                await self._sleep(0.5 * (attempt + 1))
                continue
            except ExchangeError as e:
                await self._sync_order(None, cid, str(e))
                await self._event(trade.id, f"{kind}_error", f"{otype} error: {e}", attempt=attempt)
                await self._sleep(0.5 * (attempt + 1))
                continue
            if info is None:
                await self._sync_order(None, cid, "not found after unknown status")
                continue
            await self._sync_order(info, cid)
            if await self._verify_open(cid):
                prev = prot.get(kind)
                prot[kind] = cid
                if kind == "sl":
                    prot["sl_price"] = trigger
                await self.store.update_trade(trade.id, protection=prot,
                                              **({"stop_price": trigger} if kind == "sl" else {}))
                trade.protection = prot
                label = {"sl": "SL", "tp1": "TP1", "tp2": "TP2"}[kind]
                await self._event(trade.id, f"{kind}_submitted", f"{label} {otype} {qty} @ {trigger:,.1f} verified",
                                  client_id=cid, replaced=prev)
                if prev and prev != cid:
                    await self._cancel_quiet(prev, True)
                return True
            await self._event(trade.id, f"{kind}_unverified", f"{otype} {cid} not found among open orders",
                              attempt=attempt)
        await self.store.update_trade(trade.id, protection=prot)
        trade.protection = prot
        return False

    async def _verify_open(self, cid: str) -> bool:
        for i in range(3):
            try:
                orders = await self.ex.get_open_orders(self.symbol)
                if any(o.client_order_id == cid and o.is_open for o in orders):
                    return True
            except ExchangeError as e:
                log.warning("verify open orders failed: %s", e)
            await self._sleep(0.3 * (i + 1))
        return False

    async def _cancel_quiet(self, cid: str, conditional: bool) -> bool:
        try:
            ok = await self.ex.cancel_order(self.symbol, cid, conditional)
            await self.store.update_order(cid, status="CANCELED")
            return ok
        except ExchangeError as e:
            log.warning("cancel %s failed: %s", cid, e)
            return False

    async def move_stop(self, trade: M.Trade, new_stop: float, qty: float, reason: str) -> bool:
        """Replace the stop (place new, verify, then cancel old). Old stop stays if the new one fails."""
        async with self.lock:
            ok = await self._place_protective(trade, "sl", new_stop, qty)
            if ok:
                await self._event(trade.id, "stop_moved", f"stop moved to {new_stop:,.1f} ({reason})")
            else:
                await self._event(trade.id, "stop_move_failed", f"could not move stop ({reason}); previous stop kept")
            return ok

    async def ensure_protection(self, trade: M.Trade, pos: PositionInfo, open_orders: list[OrderInfo]) -> str:
        """Verify SL/TP exist for an open position; re-place missing ones. Returns 'ok'|'repaired'|'failed'."""
        async with self.lock:
            prot = dict(trade.protection or {})
            by_id = {o.client_order_id: o for o in open_orders if o.is_open}
            sl_id = prot.get("sl")
            sl_open = sl_id in by_id
            # Qty must cover the position (reduce-only: excess is harmless, shortfall is not).
            if sl_open and by_id[sl_id].quantity + 1e-9 < pos.size:
                sl_open = False
            status = "ok"
            if not sl_open:
                await self._event(trade.id, "sl_missing", "stop-loss missing or undersized on venue — re-placing")
                stop = trade.stop_price or trade.initial_stop
                if stop is None:
                    return "failed"
                ok = await self._place_protective(trade, "sl", stop, pos.size)
                if not ok:
                    return "failed"
                status = "repaired"
            if not prot.get("software_tp"):
                for kind, price in (("tp1", trade.tp1_price), ("tp2", trade.tp2_price)):
                    if kind == "tp1" and (trade.tp1_filled or not price):
                        continue
                    tid_ = prot.get(kind)
                    if price and tid_ not in by_id:
                        qty = pos.size if kind == "tp2" and (trade.tp1_filled or not trade.tp1_price) else None
                        if qty is None:
                            orders = await self.store.trade_orders(trade.id)
                            prev = next((o for o in orders if o.client_order_id == tid_), None)
                            qty = min(prev.quantity, pos.size) if prev else pos.size
                        trade = await self.store.get_trade(trade.id)
                        if await self._place_protective(trade, kind, price, qty):
                            status = "repaired" if status == "ok" else status
            return status

    # ------------------------------------------------------------------ closing
    async def close_trade(self, trade: M.Trade, reason: str) -> bool:
        async with self.lock:
            return await self._flatten(trade, reason, emergency=False)

    async def emergency_flatten(self, reason: str) -> bool:
        """Flatten whatever position the venue holds (used by kill switch / unknown positions)."""
        async with self.lock:
            trades = await self.store.open_trades(self.mode)
            if trades:
                ok = True
                for t in trades:
                    ok = await self._flatten(t, reason, emergency=True) and ok
                return ok
            return await self._flatten(None, reason, emergency=True)

    async def _flatten(self, trade: M.Trade | None, reason: str, emergency: bool) -> bool:
        tid = trade.id if trade else None
        if trade is not None:
            await self.store.update_trade(trade.id, status="closing")
        flat = False
        for attempt in range(4):
            try:
                pos = await self._position()
            except ExchangeError as e:
                log.error("flatten: position query failed: %s", e)
                await self._sleep(1.0)
                continue
            if pos is None:
                flat = True
                break
            side = "SELL" if pos.quantity > 0 else "BUY"
            cid = self.cid(tid or 0, "X", attempt + int(utcnow().timestamp()) % 100000)
            await self._record_order(tid, "emergency" if emergency else "exit", cid, side, "MARKET", pos.size,
                                     reduce_only=True)
            try:
                info = await self.ex.place_market_order(self.symbol, side, pos.size, cid, reduce_only=True)
                await self._sync_order(info, cid)
            except UnknownOrderStatus:
                info = await self._resolve_unknown(cid, False)
                await self._sync_order(info, cid)
            except ExchangeError as e:
                await self._sync_order(None, cid, str(e))
                log.error("flatten attempt %s failed: %s", attempt, e)
            await self._sleep(0.5 * (attempt + 1))
        if flat:
            try:
                await self.ex.cancel_all_orders(self.symbol)
            except ExchangeError as e:
                log.warning("cancel-all after flatten failed: %s", e)
            await self.sync_trade_orders(tid)
            if trade is not None:
                await self.finalize(trade, "emergency" if emergency else reason.split(":")[0].replace(" ", "_")[:24])
        else:
            await self.store.system_event("critical", "execution", "flatten_failed",
                                          f"could not flatten position ({reason}); protective orders left in place")
            if trade is not None:
                await self.store.update_trade(trade.id, status="open")
        if tid:
            await self._event(tid, "flattened" if flat else "flatten_failed", reason)
        return flat

    async def sync_trade_orders(self, trade_id: int | None) -> None:
        """Record each still-open (in the DB) order's final venue status: FILLED / CANCELED / EXPIRED."""
        for o in await self.store.active_orders(self.mode):
            if trade_id is not None and o.trade_id != trade_id:
                continue
            try:
                info = await self.ex.get_order(self.symbol, o.client_order_id, o.conditional)
            except ExchangeError:
                info = None
            if info is None:
                await self.store.update_order(o.client_order_id, status="CANCELED")
            else:
                await self._sync_order(info, o.client_order_id)

    # ------------------------------------------------------------------ settlement
    async def _trade_fill_ids(self, trade: M.Trade) -> set[str]:
        """Venue order ids whose fills belong to this trade.

        Regular orders fill under their own orderId. A Binance algo (conditional) order
        fills under the *actual* order it spawns when triggered (``actualOrderId``).
        """
        ids: set[str] = set()
        for o in await self.store.trade_orders(trade.id):
            if o.venue_order_id:
                ids.add(str(o.venue_order_id))
            actual = (o.raw or {}).get("actualOrderId")
            if o.conditional and not actual and o.status not in ("CANCELED", "FAILED", "NOT_FOUND", "SUBMITTING"):
                try:
                    info = await self.ex.get_order(self.symbol, o.client_order_id, True)
                    actual = (info.raw or {}).get("actualOrderId") if info else None
                except ExchangeError:
                    actual = None
            if actual:
                ids.add(str(actual))
        return ids

    async def finalize(self, trade: M.Trade, exit_reason: str) -> M.Trade:
        """Compute P&L from venue fills + funding and mark the trade closed."""
        start_ms = int((trade.opened_at or trade.created_at).timestamp() * 1000) - 2000
        gross = fees = 0.0
        exit_qty = exit_notional = 0.0
        try:
            fills = await self.ex.get_fills(self.symbol, start_ms)
            own = await self._trade_fill_ids(trade)
            closing_side = side_for(trade.direction, closing=True)
            mine = [f for f in fills if f.order_id in own]
            # Liquidations / external closes are fills Kestrel did not place: use them only to
            # cover the part of the position our own orders did not close.
            closed_by_us = sum(f.qty for f in mine if f.side == closing_side)
            missing = max(0.0, (trade.quantity or 0.0) - closed_by_us)
            if missing > 1e-9:
                for f in sorted((f for f in fills if f.order_id not in own and f.side == closing_side),
                                key=lambda f: f.time):
                    if missing <= 1e-9:
                        break
                    mine.append(f)
                    missing -= f.qty
            for f in mine:
                fees += f.commission
                gross += f.realized_pnl
                if f.side == closing_side:
                    exit_qty += f.qty
                    exit_notional += f.qty * f.price
            funding = await self.ex.get_funding_since(self.symbol, start_ms)
        except ExchangeError as e:
            log.error("finalize: could not fetch fills for trade %s: %s", trade.id, e)
            funding = 0.0
            await self.store.system_event("warning", "execution", "pnl_estimated",
                                          f"trade #{trade.id}: fills unavailable, P&L estimated")
        exit_px = exit_notional / exit_qty if exit_qty else None
        estimated = not exit_qty
        if not exit_qty and trade.entry_price:
            mark = None
            try:
                mark = await self.ex.get_mark_price(self.symbol)
            except ExchangeError:
                pass
            exit_px = mark
            if mark:
                d = 1 if trade.direction == "LONG" else -1
                gross = d * (mark - trade.entry_price) * trade.quantity
        net = gross - fees + funding
        pnl_pct = net / trade.equity_at_entry * 100 if trade.equity_at_entry else None
        r_mult = net / trade.risk_usdt if trade.risk_usdt else None
        t = await self.store.update_trade(trade.id, status="closed", closed_at=utcnow(), exit_price=exit_px,
                                          exit_reason=exit_reason, gross_pnl=gross, fees=fees, funding=funding,
                                          realized_pnl=net, pnl_pct=pnl_pct, r_multiple=r_mult, remaining_qty=0.0,
                                          **({"notes": "P&L estimated from mark price: no closing fills were found "
                                                       "on the venue."} if estimated else {}))
        await self._event(trade.id, "closed", f"closed ({exit_reason}) @ {exit_px or 0:,.2f}",
                          exit_price=exit_px, reason=exit_reason)
        await self._event(trade.id, "pnl_calculated", f"net P&L {net:+,.2f} USDT (gross {gross:+,.2f}, fees "
                                                      f"{fees:,.2f}, funding {funding:+,.2f})",
                          gross=gross, fees=fees, funding=funding, net=net, r=r_mult)
        return t


def _slim(raw: dict[str, Any]) -> dict[str, Any]:
    keep = ("orderId", "algoId", "clientOrderId", "clientAlgoId", "status", "algoStatus", "type", "orderType",
            "side", "origQty", "quantity", "executedQty", "avgPrice", "stopPrice", "triggerPrice", "actualOrderId",
            "actualPrice", "updateTime", "reduceOnly")
    return {k: raw.get(k) for k in keep if k in (raw or {})}
