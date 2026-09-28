"""Execution adapter interface.

Paper and live trading share *everything* above this interface — strategy, risk
engine, execution engine, position monitor, reconciliation. Only the adapter
changes. A different exchange can be supported by implementing this protocol.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.exchange.models import AccountInfo, BookTicker, Fill, OrderInfo, PositionInfo, SymbolFilters


class ExecutionAdapter(ABC):
    name: str = "abstract"
    mode: str = "paper"  # paper | live

    @abstractmethod
    async def get_filters(self, symbol: str) -> SymbolFilters: ...

    @abstractmethod
    async def get_account(self) -> AccountInfo: ...

    @abstractmethod
    async def get_position(self, symbol: str) -> PositionInfo | None:
        """Current position or ``None`` when flat."""

    @abstractmethod
    async def get_open_orders(self, symbol: str) -> list[OrderInfo]:
        """All open orders including conditional (stop / take-profit) orders."""

    @abstractmethod
    async def get_order(self, symbol: str, client_order_id: str, conditional: bool) -> OrderInfo | None: ...

    @abstractmethod
    async def get_mark_price(self, symbol: str) -> float: ...

    @abstractmethod
    async def get_book_ticker(self, symbol: str) -> BookTicker | None: ...

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> int:
        """Set leverage; returns the leverage the venue confirmed."""

    @abstractmethod
    async def ensure_isolated_margin(self, symbol: str) -> None: ...

    @abstractmethod
    async def place_market_order(
        self, symbol: str, side: str, quantity: float, client_order_id: str, reduce_only: bool = False
    ) -> OrderInfo: ...

    @abstractmethod
    async def place_stop_market(
        self, symbol: str, side: str, trigger_price: float, quantity: float, client_order_id: str
    ) -> OrderInfo:
        """Reduce-only stop-market (mark-price triggered) protective order."""

    @abstractmethod
    async def place_take_profit_market(
        self, symbol: str, side: str, trigger_price: float, quantity: float, client_order_id: str
    ) -> OrderInfo:
        """Reduce-only take-profit-market (mark-price triggered) order."""

    @abstractmethod
    async def cancel_order(self, symbol: str, client_order_id: str, conditional: bool) -> bool: ...

    @abstractmethod
    async def cancel_all_orders(self, symbol: str) -> None:
        """Cancel every open order, regular and conditional."""

    @abstractmethod
    async def get_fills(self, symbol: str, start_ms: int) -> list[Fill]: ...

    @abstractmethod
    async def get_funding_since(self, symbol: str, start_ms: int) -> float:
        """Net funding received (+) / paid (-) since ``start_ms``."""

    async def server_time_offset_ms(self) -> int:
        return 0

    async def close(self) -> None:  # pragma: no cover - optional
        return None
