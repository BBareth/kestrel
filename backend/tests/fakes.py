"""Fault-injecting venue + recorders used by execution / reconciliation tests."""

from __future__ import annotations

from collections import deque
from typing import Any

from app.exchange.models import ExchangeError, OrderRejected, UnknownOrderStatus
from app.exchange.paper import PaperExchange, StaticPrices
from app.execution.engine import ExecutionEngine, TradePlan
from app.execution.monitor import PositionMonitor, Reconciler

SYM = "BTCUSDT"


class FlakyVenue(PaperExchange):
    """PaperExchange that can misbehave like a real exchange.

    * ``fail[method] = (n, exc)``: raise ``exc`` for the next n calls
    * ``ghost[method] = n``: perform the call, then raise UnknownOrderStatus (request reached
      the matching engine but the response was lost) for the next n calls
    * ``down = True``: every call raises ExchangeError (exchange unreachable)
    """

    def __init__(self, price: float = 84_000.0, balance: float = 10_000.0, mode: str = "paper") -> None:
        self.px = StaticPrices()
        self.px.set(price, spread=0.2)
        super().__init__(self.px, starting_balance=balance)
        self.mode = mode
        self.fail: dict[str, tuple[int, Exception]] = {}
        self.ghost: dict[str, int] = {}
        self.down = False
        self.calls: list[str] = []

    def _maybe_fail(self, name: str) -> None:
        self.calls.append(name)
        if self.down:
            raise ExchangeError("exchange unreachable (simulated)")
        n_exc = self.fail.get(name)
        if n_exc and n_exc[0] > 0:
            self.fail[name] = (n_exc[0] - 1, n_exc[1])
            raise n_exc[1]

    def _ghost(self, name: str) -> bool:
        n = self.ghost.get(name, 0)
        if n > 0:
            self.ghost[name] = n - 1
            return True
        return False

    async def get_account(self):  # noqa: ANN201
        self._maybe_fail("get_account")
        return await super().get_account()

    async def get_position(self, symbol):  # noqa: ANN001, ANN201
        self._maybe_fail("get_position")
        return await super().get_position(symbol)

    async def get_open_orders(self, symbol):  # noqa: ANN001, ANN201
        self._maybe_fail("get_open_orders")
        return await super().get_open_orders(symbol)

    async def get_order(self, symbol, cid, conditional):  # noqa: ANN001, ANN201
        self._maybe_fail("get_order")
        return await super().get_order(symbol, cid, conditional)

    async def place_market_order(self, symbol, side, quantity, client_order_id, reduce_only=False):  # noqa: ANN001, ANN201
        self._maybe_fail("place_market_order")
        r = await super().place_market_order(symbol, side, quantity, client_order_id, reduce_only)
        if self._ghost("place_market_order"):
            raise UnknownOrderStatus("simulated timeout after execution")
        return r

    async def place_stop_market(self, symbol, side, trigger_price, quantity, client_order_id):  # noqa: ANN001, ANN201
        self._maybe_fail("place_stop_market")
        r = await super().place_stop_market(symbol, side, trigger_price, quantity, client_order_id)
        if self._ghost("place_stop_market"):
            raise UnknownOrderStatus("simulated timeout after execution")
        return r

    async def place_take_profit_market(self, symbol, side, trigger_price, quantity, client_order_id):  # noqa: ANN001, ANN201
        self._maybe_fail("place_take_profit_market")
        return await super().place_take_profit_market(symbol, side, trigger_price, quantity, client_order_id)

    async def cancel_order(self, symbol, cid, conditional):  # noqa: ANN001, ANN201
        self._maybe_fail("cancel_order")
        return await super().cancel_order(symbol, cid, conditional)

    async def cancel_all_orders(self, symbol):  # noqa: ANN001, ANN201
        self._maybe_fail("cancel_all_orders")
        return await super().cancel_all_orders(symbol)

    async def set_leverage(self, symbol, leverage):  # noqa: ANN001, ANN201
        self._maybe_fail("set_leverage")
        return await super().set_leverage(symbol, leverage)

    def external_cancel(self, cid: str) -> None:
        """Order disappears without Kestrel's involvement (manual cancel / venue expiry)."""
        o = self.state.orders.pop(cid)
        o.status = "CANCELED"
        self.state.history.append(o)

    def open_ids(self) -> list[str]:
        return [o.client_order_id for o in self.state.orders.values() if o.status == "NEW"]


class Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, str, str]] = []

        class _S:
            configured = True

        self.sender = _S()

    async def notify(self, category: str, title: str, body: str, severity: str = "info",
                     data: dict[str, Any] | None = None) -> None:
        self.sent.append((category, title, body, severity))

    def categories(self) -> list[str]:
        return [c for c, *_ in self.sent]


class Halts:
    def __init__(self) -> None:
        self.reasons: list[str] = []

    async def __call__(self, reason: str) -> None:
        self.reasons.append(reason)


async def _no_sleep(_: float) -> None:
    return None


def rig(store, venue: FlakyVenue | None = None, salt: str = "t1"):  # noqa: ANN001, ANN201
    venue = venue or FlakyVenue()
    notes, halts = Recorder(), Halts()
    x = ExecutionEngine(venue, store, notes, SYM, halts, salt=salt, sleep=_no_sleep, verify_attempts=2)
    mon = PositionMonitor(x, store, notes, halts)
    rec = Reconciler(x, mon, store, notes, halts)
    return venue, x, mon, rec, notes, halts


def plan(direction: str = "LONG", entry: float = 84_000.0, **kw: Any) -> TradePlan:
    d = 1 if direction == "LONG" else -1
    base = dict(signal_id=None, direction=direction, entry=entry, stop=entry - d * 200, tp1=entry + d * 300,
                tp2=entry + d * 600, quantity=0.1, tp1_qty=0.05, tp2_qty=0.05, split=True, leverage=3,
                risk_usdt=25.0, equity=10_000.0, max_slippage_bps=8.0, min_rr=2.0)
    base.update(kw)
    return TradePlan(**base)


SAFETY = {"unexpected_position_policy": "protect", "emergency_stop_pct": 1.5, "kill_positions": "close",
          "kill_cancel_orders": True, "kill_to_paper": True}

_ = deque  # re-export guard for linters
_ = OrderRejected
