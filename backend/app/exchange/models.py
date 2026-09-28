"""Exchange-neutral data types used by the execution layer."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal
from typing import Any, Literal

Side = Literal["BUY", "SELL"]
Direction = Literal["LONG", "SHORT"]


class ExchangeError(Exception):
    """Base error for exchange calls."""

    def __init__(self, message: str, code: int | None = None, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class UnknownOrderStatus(ExchangeError):
    """The request was sent but the outcome is unknown (timeout / 503). Must be resolved by querying."""


class RateLimited(ExchangeError):
    pass


class AuthError(ExchangeError):
    pass


class OrderRejected(ExchangeError):
    pass


def side_for(direction: str, closing: bool = False) -> Side:
    opening = "BUY" if direction == "LONG" else "SELL"
    if not closing:
        return opening  # type: ignore[return-value]
    return "SELL" if opening == "BUY" else "BUY"


@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    tick_size: Decimal
    step_size: Decimal
    min_qty: Decimal
    max_qty: Decimal
    market_step_size: Decimal
    market_min_qty: Decimal
    market_max_qty: Decimal
    min_notional: Decimal
    price_precision: int = 1
    quantity_precision: int = 3

    def round_price(self, price: float) -> Decimal:
        d = Decimal(str(price))
        return (d / self.tick_size).quantize(Decimal(1), rounding=ROUND_HALF_UP) * self.tick_size

    def round_qty_down(self, qty: float, market: bool = True) -> Decimal:
        step = self.market_step_size if market else self.step_size
        d = Decimal(str(qty))
        return (d / step).quantize(Decimal(1), rounding=ROUND_DOWN) * step

    def fmt_price(self, price: float) -> str:
        return format(self.round_price(price).normalize(), "f")

    def fmt_qty(self, qty: float, market: bool = True) -> str:
        return format(self.round_qty_down(qty, market).normalize(), "f")

    def min_qty_for(self, market: bool = True) -> float:
        return float(self.market_min_qty if market else self.min_qty)


DEFAULT_BTCUSDT_FILTERS = SymbolFilters(
    symbol="BTCUSDT",
    tick_size=Decimal("0.10"),
    step_size=Decimal("0.001"),
    min_qty=Decimal("0.001"),
    max_qty=Decimal("1000"),
    market_step_size=Decimal("0.001"),
    market_min_qty=Decimal("0.001"),
    market_max_qty=Decimal("120"),
    min_notional=Decimal("100"),
    price_precision=2,
    quantity_precision=3,
)


@dataclass
class AccountInfo:
    equity: float  # margin balance (wallet + unrealised)
    wallet_balance: float
    available_balance: float
    unrealized_pnl: float
    asset: str = "USDT"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class PositionInfo:
    symbol: str
    quantity: float  # signed: + long, - short
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    liquidation_price: float | None
    leverage: int | None
    margin: float | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def direction(self) -> str | None:
        if self.quantity > 0:
            return "LONG"
        if self.quantity < 0:
            return "SHORT"
        return None

    @property
    def size(self) -> float:
        return abs(self.quantity)


@dataclass
class OrderInfo:
    symbol: str
    client_order_id: str
    venue_order_id: str
    side: str
    type: str
    status: str  # NEW | PARTIALLY_FILLED | FILLED | CANCELED | EXPIRED | REJECTED | TRIGGERED...
    quantity: float
    filled_qty: float
    avg_price: float | None
    price: float | None = None
    trigger_price: float | None = None
    reduce_only: bool = False
    close_position: bool = False
    conditional: bool = False
    update_time: int = 0
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.status in ("NEW", "PARTIALLY_FILLED")

    @property
    def is_filled(self) -> bool:
        return self.status == "FILLED"


@dataclass
class Fill:
    order_id: str
    side: str
    price: float
    qty: float
    commission: float
    realized_pnl: float
    time: int
    maker: bool = False


@dataclass
class BookTicker:
    bid: float
    ask: float
    ts: int

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2

    @property
    def spread_bps(self) -> float:
        m = self.mid
        return (self.ask - self.bid) / m * 10_000 if m else 0.0
