"""Paper-trading exchange implementing the same ExecutionAdapter as live Binance.

Realism model:
* Market orders fill at the book (ask for buys, bid for sells; last price when the
  book is unknown) plus adverse slippage, and pay the taker fee.
* Stop / take-profit orders are reduce-only conditional orders triggered by the
  MARK price (like ``workingType=MARK_PRICE`` live). Stops fill at the worse of
  trigger and current price plus extra stop slippage (gaps hurt, as in reality).
* Isolated margin: opening requires initial margin ≤ available balance; the
  position is liquidated at the isolated liquidation price if mark crosses it.
* Funding is applied at each funding timestamp: longs pay a positive rate.
* Orders that would trigger immediately are rejected, like Binance (-2021).

State is serialisable (``snapshot`` / ``restore``) so paper positions and orders
survive restarts exactly like exchange-side state does in live mode.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from app.exchange.base import ExecutionAdapter
from app.exchange.models import (
    DEFAULT_BTCUSDT_FILTERS,
    AccountInfo,
    BookTicker,
    ExchangeError,
    Fill,
    OrderInfo,
    OrderRejected,
    PositionInfo,
    SymbolFilters,
)

MAINT_MARGIN_RATE = 0.004  # BTCUSDT first notional bracket
LIQUIDATION_FEE_RATE = 0.005


class PriceSource(Protocol):
    def mark_price(self) -> float | None: ...
    def last_price(self) -> float | None: ...
    def book(self) -> BookTicker | None: ...


@dataclass
class StaticPrices:
    """Simple mutable price source (tests, backtests)."""

    mark: float | None = None
    last: float | None = None
    bid: float | None = None
    ask: float | None = None

    def mark_price(self) -> float | None:
        return self.mark

    def last_price(self) -> float | None:
        return self.last if self.last is not None else self.mark

    def book(self) -> BookTicker | None:
        if self.bid is None or self.ask is None:
            return None
        return BookTicker(bid=self.bid, ask=self.ask, ts=int(time.time() * 1000))

    def set(self, price: float, spread: float = 0.1) -> None:
        self.mark = price
        self.last = price
        self.bid = price - spread / 2
        self.ask = price + spread / 2


@dataclass
class PaperOrder:
    client_order_id: str
    venue_order_id: str
    symbol: str
    side: str
    type: str
    quantity: float
    trigger_price: float | None
    reduce_only: bool
    conditional: bool
    status: str = "NEW"
    filled_qty: float = 0.0
    avg_price: float | None = None
    created_ms: int = 0
    update_ms: int = 0

    def info(self) -> OrderInfo:
        return OrderInfo(
            symbol=self.symbol, client_order_id=self.client_order_id, venue_order_id=self.venue_order_id,
            side=self.side, type=self.type, status=self.status, quantity=self.quantity, filled_qty=self.filled_qty,
            avg_price=self.avg_price, trigger_price=self.trigger_price, reduce_only=self.reduce_only,
            conditional=self.conditional, update_time=self.update_ms,
        )


@dataclass
class PaperPosition:
    quantity: float = 0.0  # signed
    entry_price: float = 0.0
    margin: float = 0.0  # isolated margin allocated
    opened_ms: int = 0


@dataclass
class PaperState:
    wallet_balance: float
    leverage: int = 3
    position: PaperPosition = field(default_factory=PaperPosition)
    orders: dict[str, PaperOrder] = field(default_factory=dict)
    history: list[PaperOrder] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    funding: list[tuple[int, float]] = field(default_factory=list)
    last_funding_ms: int = 0
    seq: int = 0
    run: str = field(default_factory=lambda: secrets.token_hex(3))  # makes venue ids unique across resets
    total_fees: float = 0.0
    total_funding: float = 0.0
    liquidations: int = 0


class PaperExchange(ExecutionAdapter):
    name = "paper"
    mode = "paper"

    def __init__(self, prices: PriceSource, starting_balance: float = 10_000.0, symbol: str = "BTCUSDT",
                 taker_fee_pct: float = 0.05, slippage_bps: float = 2.0, stop_slippage_bps: float = 5.0,
                 filters: SymbolFilters = DEFAULT_BTCUSDT_FILTERS,
                 persist: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
                 clock: Callable[[], int] | None = None) -> None:
        self.prices = prices
        self.symbol = symbol
        self.filters = filters
        self.taker_fee = taker_fee_pct / 100
        self.slippage = slippage_bps / 10_000
        self.stop_slippage = stop_slippage_bps / 10_000
        self.state = PaperState(wallet_balance=starting_balance)
        self._persist = persist
        self._lock = asyncio.Lock()
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.fail_next: dict[str, Exception] = {}  # test hook: method name → exception to raise once
        self._pending_funding: tuple[int, float] | None = None

    # ------------------------------------------------------------------ config / persistence
    def configure(self, taker_fee_pct: float, slippage_bps: float, stop_slippage_bps: float) -> None:
        self.taker_fee = taker_fee_pct / 100
        self.slippage = slippage_bps / 10_000
        self.stop_slippage = stop_slippage_bps / 10_000

    def snapshot(self) -> dict[str, Any]:
        s = self.state
        return {
            "wallet_balance": s.wallet_balance,
            "leverage": s.leverage,
            "position": asdict(s.position),
            "orders": {k: asdict(v) for k, v in s.orders.items()},
            "history": [asdict(o) for o in s.history[-300:]],
            "fills": [asdict(f) for f in s.fills[-1000:]],
            "funding": [list(x) for x in s.funding[-500:]],
            "last_funding_ms": s.last_funding_ms,
            "seq": s.seq,
            "run": s.run,
            "total_fees": s.total_fees,
            "total_funding": s.total_funding,
            "liquidations": s.liquidations,
        }

    def restore(self, snap: dict[str, Any]) -> None:
        self.state = PaperState(
            wallet_balance=float(snap["wallet_balance"]),
            leverage=int(snap.get("leverage", 3)),
            position=PaperPosition(**snap.get("position", {})),
            orders={k: PaperOrder(**v) for k, v in snap.get("orders", {}).items()},
            history=[PaperOrder(**o) for o in snap.get("history", [])],
            fills=[Fill(**f) for f in snap.get("fills", [])],
            funding=[(int(a), float(b)) for a, b in snap.get("funding", [])],
            last_funding_ms=int(snap.get("last_funding_ms", 0)),
            seq=int(snap.get("seq", 0)),
            run=str(snap.get("run") or secrets.token_hex(3)),
            total_fees=float(snap.get("total_fees", 0.0)),
            total_funding=float(snap.get("total_funding", 0.0)),
            liquidations=int(snap.get("liquidations", 0)),
        )

    def reset(self, balance: float) -> None:
        self.state = PaperState(wallet_balance=balance, leverage=self.state.leverage)

    async def _save(self) -> None:
        if self._persist:
            await self._persist(self.snapshot())

    def _check_fail(self, method: str) -> None:
        exc = self.fail_next.pop(method, None)
        if exc is not None:
            raise exc

    # ------------------------------------------------------------------ prices
    def _mark(self) -> float:
        m = self.prices.mark_price()
        if not m or m <= 0:
            raise ExchangeError("paper exchange has no market price (market data unavailable)")
        return m

    def _last(self) -> float:
        p = self.prices.last_price() or self.prices.mark_price()
        if not p or p <= 0:
            raise ExchangeError("paper exchange has no market price (market data unavailable)")
        return p

    def _market_fill_price(self, side: str) -> float:
        book = self.prices.book()
        if book and book.bid > 0 and book.ask > 0:
            base = book.ask if side == "BUY" else book.bid
        else:
            base = self._last()
        return base * (1 + self.slippage) if side == "BUY" else base * (1 - self.slippage)

    # ------------------------------------------------------------------ interface: info
    async def get_filters(self, symbol: str) -> SymbolFilters:
        return self.filters

    def _unrealized(self, mark: float) -> float:
        p = self.state.position
        return (mark - p.entry_price) * p.quantity if p.quantity else 0.0

    async def get_account(self) -> AccountInfo:
        self._check_fail("get_account")
        s = self.state
        try:
            upnl = self._unrealized(self._mark())
        except ExchangeError:
            upnl = 0.0
        return AccountInfo(
            equity=s.wallet_balance + upnl,
            wallet_balance=s.wallet_balance,
            available_balance=max(0.0, s.wallet_balance - s.position.margin),
            unrealized_pnl=upnl,
        )

    def liquidation_price(self) -> float | None:
        p = self.state.position
        if not p.quantity:
            return None
        q = abs(p.quantity)
        m = p.margin
        if p.quantity > 0:
            return max(0.0, (p.entry_price * q - m) / (q * (1 - MAINT_MARGIN_RATE)))
        return (p.entry_price * q + m) / (q * (1 + MAINT_MARGIN_RATE))

    async def get_position(self, symbol: str) -> PositionInfo | None:
        self._check_fail("get_position")
        p = self.state.position
        if not p.quantity:
            return None
        try:
            mark = self._mark()
        except ExchangeError:
            mark = p.entry_price
        return PositionInfo(
            symbol=symbol, quantity=p.quantity, entry_price=p.entry_price, mark_price=mark,
            unrealized_pnl=self._unrealized(mark), liquidation_price=self.liquidation_price(),
            leverage=self.state.leverage, margin=p.margin,
        )

    async def get_open_orders(self, symbol: str) -> list[OrderInfo]:
        self._check_fail("get_open_orders")
        return [o.info() for o in self.state.orders.values() if o.status in ("NEW", "PARTIALLY_FILLED")]

    async def get_order(self, symbol: str, client_order_id: str, conditional: bool) -> OrderInfo | None:
        self._check_fail("get_order")
        o = self.state.orders.get(client_order_id)
        if o:
            return o.info()
        for h in reversed(self.state.history):
            if h.client_order_id == client_order_id:
                return h.info()
        return None

    async def get_mark_price(self, symbol: str) -> float:
        return self._mark()

    async def get_book_ticker(self, symbol: str) -> BookTicker | None:
        return self.prices.book()

    async def set_leverage(self, symbol: str, leverage: int) -> int:
        self._check_fail("set_leverage")
        if self.state.position.quantity and leverage != self.state.leverage:
            # Binance allows increasing leverage on an open isolated position; keep it simple and refuse.
            raise OrderRejected("cannot change paper leverage with an open position", code=-4161)
        self.state.leverage = max(1, int(leverage))
        await self._save()
        return self.state.leverage

    async def ensure_isolated_margin(self, symbol: str) -> None:
        return None

    # ------------------------------------------------------------------ fills
    def _next_id(self) -> str:
        self.state.seq += 1
        return f"P{self.state.run}-{self.state.seq}"

    def _apply_fill(self, side: str, qty: float, price: float, order_id: str, fee_rate: float) -> float:
        """Apply a fill to the position; returns realised PnL (before fees)."""
        s = self.state
        p = s.position
        signed = qty if side == "BUY" else -qty
        now = self._clock()
        realized = 0.0
        fee = qty * price * fee_rate
        if p.quantity == 0 or (p.quantity > 0) == (signed > 0):
            # open / increase
            new_qty = p.quantity + signed
            p.entry_price = (p.entry_price * abs(p.quantity) + price * qty) / abs(new_qty)
            p.margin += qty * price / s.leverage
            if p.quantity == 0:
                p.opened_ms = now
            p.quantity = new_qty
        else:
            # reduce / close (reduce-only paths never flip)
            close_qty = min(qty, abs(p.quantity))
            direction = 1 if p.quantity > 0 else -1
            realized = (price - p.entry_price) * close_qty * direction
            frac = close_qty / abs(p.quantity)
            released = p.margin * frac
            p.margin -= released
            p.quantity += close_qty if p.quantity < 0 else -close_qty
            if abs(p.quantity) < 1e-12:
                p.quantity = 0.0
                p.entry_price = 0.0
                p.margin = 0.0
                p.opened_ms = 0
        s.wallet_balance += realized - fee
        s.total_fees += fee
        s.fills.append(Fill(order_id=order_id, side=side, price=price, qty=qty, commission=fee,
                            realized_pnl=realized, time=now))
        return realized

    # ------------------------------------------------------------------ orders
    async def place_market_order(self, symbol: str, side: str, quantity: float, client_order_id: str,
                                 reduce_only: bool = False) -> OrderInfo:
        async with self._lock:
            self._check_fail("place_market_order")
            if client_order_id in self.state.orders or any(
                    h.client_order_id == client_order_id for h in self.state.history[-300:]):
                raise OrderRejected("duplicate clientOrderId", code=-4015)
            qty = float(self.filters.round_qty_down(quantity))
            if qty < self.filters.min_qty_for():
                raise OrderRejected("quantity below minimum", code=-4003)
            price = self._market_fill_price(side)
            p = self.state.position
            closing = p.quantity != 0 and ((p.quantity > 0) != (side == "BUY"))
            now = self._clock()
            if reduce_only:
                if not closing:
                    raise OrderRejected("ReduceOnly Order is rejected.", code=-2022)
                qty = min(qty, abs(p.quantity))
            elif not closing:
                margin_needed = qty * price / self.state.leverage
                available = self.state.wallet_balance - p.margin
                if margin_needed + qty * price * self.taker_fee > available:
                    raise OrderRejected("Margin is insufficient.", code=-2019)
                if qty * price < float(self.filters.min_notional):
                    raise OrderRejected("Order's notional must be no smaller than min notional", code=-4164)
            elif qty > abs(p.quantity) + 1e-12:
                raise OrderRejected("paper exchange does not flip positions in one order", code=-2022)
            order = PaperOrder(client_order_id=client_order_id, venue_order_id=self._next_id(), symbol=symbol,
                               side=side, type="MARKET", quantity=qty, trigger_price=None, reduce_only=reduce_only,
                               conditional=False, status="FILLED", filled_qty=qty, avg_price=price,
                               created_ms=now, update_ms=now)
            self._apply_fill(side, qty, price, order.venue_order_id, self.taker_fee)
            self.state.history.append(order)
            await self._save()
            return order.info()

    async def _place_conditional(self, symbol: str, side: str, order_type: str, trigger: float, quantity: float,
                                 client_order_id: str) -> OrderInfo:
        async with self._lock:
            self._check_fail(f"place_{order_type.lower()}")
            if client_order_id in self.state.orders:
                raise OrderRejected("duplicate clientAlgoId", code=-4015)
            mark = self._mark()
            trig = float(self.filters.round_price(trigger))
            immediate = (
                (order_type == "STOP_MARKET" and ((side == "SELL" and trig >= mark) or (side == "BUY" and trig <= mark)))
                or (order_type == "TAKE_PROFIT_MARKET"
                    and ((side == "SELL" and trig <= mark) or (side == "BUY" and trig >= mark)))
            )
            if immediate:
                raise OrderRejected("Order would immediately trigger.", code=-2021)
            qty = float(self.filters.round_qty_down(quantity))
            now = self._clock()
            order = PaperOrder(client_order_id=client_order_id, venue_order_id=self._next_id(), symbol=symbol,
                               side=side, type=order_type, quantity=qty, trigger_price=trig, reduce_only=True,
                               conditional=True, created_ms=now, update_ms=now)
            self.state.orders[client_order_id] = order
            await self._save()
            return order.info()

    async def place_stop_market(self, symbol: str, side: str, trigger_price: float, quantity: float,
                                client_order_id: str) -> OrderInfo:
        return await self._place_conditional(symbol, side, "STOP_MARKET", trigger_price, quantity, client_order_id)

    async def place_take_profit_market(self, symbol: str, side: str, trigger_price: float, quantity: float,
                                       client_order_id: str) -> OrderInfo:
        return await self._place_conditional(symbol, side, "TAKE_PROFIT_MARKET", trigger_price, quantity,
                                             client_order_id)

    def _close_order(self, o: PaperOrder, status: str) -> None:
        o.status = status
        o.update_ms = self._clock()
        self.state.orders.pop(o.client_order_id, None)
        self.state.history.append(o)

    async def cancel_order(self, symbol: str, client_order_id: str, conditional: bool) -> bool:
        async with self._lock:
            self._check_fail("cancel_order")
            o = self.state.orders.get(client_order_id)
            if not o:
                return False
            self._close_order(o, "CANCELED")
            await self._save()
            return True

    async def cancel_all_orders(self, symbol: str) -> None:
        async with self._lock:
            self._check_fail("cancel_all_orders")
            for o in list(self.state.orders.values()):
                self._close_order(o, "CANCELED")
            await self._save()

    async def get_fills(self, symbol: str, start_ms: int) -> list[Fill]:
        return [f for f in self.state.fills if f.time >= start_ms]

    async def get_funding_since(self, symbol: str, start_ms: int) -> float:
        return sum(a for t, a in self.state.funding if t >= start_ms)

    # ------------------------------------------------------------------ simulation tick
    async def on_market(self, funding_rate: float | None = None, next_funding_ms: int | None = None,
                        now_ms: int | None = None) -> list[str]:
        """Process triggers, liquidation and funding at the current prices.

        Returns a list of event strings (e.g. ``"filled:<client_id>"``) for logging.
        """
        events: list[str] = []
        async with self._lock:
            try:
                mark = self._mark()
            except ExchangeError:
                return events
            now = now_ms or self._clock()
            changed = False
            s = self.state
            p = s.position

            # Liquidation check first (isolated)
            liq = self.liquidation_price()
            if p.quantity and liq is not None and (
                    (p.quantity > 0 and mark <= liq) or (p.quantity < 0 and mark >= liq)):
                qty = abs(p.quantity)
                side = "SELL" if p.quantity > 0 else "BUY"
                oid = self._next_id()
                self._apply_fill(side, qty, liq, oid, LIQUIDATION_FEE_RATE)
                s.liquidations += 1
                s.history.append(PaperOrder(client_order_id=f"liq-{oid}", venue_order_id=oid, symbol=self.symbol,
                                            side=side, type="LIQUIDATION", quantity=qty, trigger_price=liq,
                                            reduce_only=True, conditional=False, status="FILLED", filled_qty=qty,
                                            avg_price=liq, created_ms=now, update_ms=now))
                events.append(f"liquidated:{liq:.2f}")
                changed = True

            # Conditional orders (stops before take-profits: conservative when both qualify)
            ordered = sorted(s.orders.values(), key=lambda o: 0 if o.type == "STOP_MARKET" else 1)
            for o in ordered:
                if o.status != "NEW" or o.trigger_price is None:
                    continue
                trig = o.trigger_price
                hit = False
                if o.type == "STOP_MARKET":
                    hit = (o.side == "SELL" and mark <= trig) or (o.side == "BUY" and mark >= trig)
                elif o.type == "TAKE_PROFIT_MARKET":
                    hit = (o.side == "SELL" and mark >= trig) or (o.side == "BUY" and mark <= trig)
                if not hit:
                    continue
                pos = s.position
                closing = pos.quantity != 0 and ((pos.quantity > 0) != (o.side == "BUY"))
                if not closing:
                    self._close_order(o, "EXPIRED")  # reduce-only with nothing to reduce
                    events.append(f"expired:{o.client_order_id}")
                    changed = True
                    continue
                qty = min(o.quantity, abs(pos.quantity))
                last = self._last()
                if o.type == "STOP_MARKET":
                    base = min(trig, last) if o.side == "SELL" else max(trig, last)
                    slip = self.slippage + self.stop_slippage
                else:
                    base = last
                    slip = self.slippage
                price = base * (1 - slip) if o.side == "SELL" else base * (1 + slip)
                self._apply_fill(o.side, qty, price, o.venue_order_id, self.taker_fee)
                o.filled_qty = qty
                o.avg_price = price
                self._close_order(o, "FILLED")
                events.append(f"filled:{o.client_order_id}")
                changed = True

            # Funding: the feed announces the next funding time T and the rate that will
            # apply. Once T has passed (clock reached it, or the feed moved on to the next
            # interval), settle T once with the last rate announced for it.
            pend = self._pending_funding
            if pend and s.last_funding_ms < pend[0] and (
                    now >= pend[0] or (next_funding_ms is not None and next_funding_ms > pend[0])):
                t_due, rate = pend
                s.last_funding_ms = t_due
                if s.position.quantity:
                    amount = -s.position.quantity * mark * rate
                    s.wallet_balance += amount
                    s.total_funding += amount
                    s.funding.append((now, amount))
                    events.append(f"funding:{amount:.4f}")
                changed = True
            if next_funding_ms and funding_rate is not None and next_funding_ms > s.last_funding_ms:
                self._pending_funding = (next_funding_ms, funding_rate)
            if changed:
                await self._save()
        return events
