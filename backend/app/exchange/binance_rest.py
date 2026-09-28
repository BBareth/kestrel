"""Binance USDⓈ-M Futures REST client (verified against the 2026 API docs).

Notable current-API facts this client relies on:
* Conditional orders (STOP_MARKET / TAKE_PROFIT_MARKET) moved to the Algo Order
  service on 2025-12-09: ``POST /fapi/v1/algoOrder`` with ``algoType=CONDITIONAL``.
  The old ``/fapi/v1/order`` path now answers -4120 STOP_ORDER_SWITCH_ALGO.
* Account / positions use the v3 endpoints (``/fapi/v3/account``,
  ``/fapi/v3/positionRisk``); v2 is deprecated. v3 positions omit leverage,
  which comes from ``/fapi/v1/symbolConfig``.
* A 503 "Unknown error" / -1007 means the order may or may not exist: it must be
  resolved by querying, never by blindly retrying.
* API-key permissions (withdrawals, IP restriction) are only visible through the
  spot SAPI ``/sapi/v1/account/apiRestrictions``.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import time
from collections import deque
from decimal import Decimal
from typing import Any
from urllib.parse import urlencode

import httpx

from app.exchange.base import ExecutionAdapter
from app.exchange.models import (
    AccountInfo,
    AuthError,
    BookTicker,
    ExchangeError,
    Fill,
    OrderInfo,
    OrderRejected,
    PositionInfo,
    RateLimited,
    SymbolFilters,
    UnknownOrderStatus,
)

log = logging.getLogger("kestrel.binance")

UNKNOWN_STATUS_CODES = {-1007, -1006}  # timeout waiting for backend / unexpected response
NOT_FOUND_CODES = {-2011, -2013, -4108}  # unknown order / order does not exist


class ErrorTracker:
    """Rolling log of API errors for the safety supervisor."""

    def __init__(self) -> None:
        self._errors: deque[tuple[float, str]] = deque(maxlen=500)

    def record(self, what: str) -> None:
        self._errors.append((time.time(), what))

    def count(self, window_s: float = 300) -> int:
        cutoff = time.time() - window_s
        return sum(1 for t, _ in self._errors if t >= cutoff)

    def recent(self, n: int = 10) -> list[tuple[float, str]]:
        return list(self._errors)[-n:]


def _f(v: Any, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def parse_filters(sym: dict[str, Any]) -> SymbolFilters:
    f = {x["filterType"]: x for x in sym.get("filters", [])}
    lot = f.get("LOT_SIZE", {})
    mlot = f.get("MARKET_LOT_SIZE", lot)
    return SymbolFilters(
        symbol=sym["symbol"],
        tick_size=Decimal(f.get("PRICE_FILTER", {}).get("tickSize", "0.1")),
        step_size=Decimal(lot.get("stepSize", "0.001")),
        min_qty=Decimal(lot.get("minQty", "0.001")),
        max_qty=Decimal(lot.get("maxQty", "1000")),
        market_step_size=Decimal(mlot.get("stepSize", "0.001")),
        market_min_qty=Decimal(mlot.get("minQty", "0.001")),
        market_max_qty=Decimal(mlot.get("maxQty", "120")),
        min_notional=Decimal(f.get("MIN_NOTIONAL", {}).get("notional", "100")),
        price_precision=int(sym.get("pricePrecision", 2)),
        quantity_precision=int(sym.get("quantityPrecision", 3)),
    )


class BinancePublic:
    """Unauthenticated market-data endpoints."""

    def __init__(self, base_url: str = "https://fapi.binance.com", timeout: float = 10.0,
                 client: httpx.AsyncClient | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client or httpx.AsyncClient(base_url=self.base_url, timeout=timeout,
                                                    headers={"User-Agent": "kestrel/1.0"})
        self.errors = ErrorTracker()

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        try:
            r = await self._client.get(path, params=params)
        except httpx.HTTPError as e:
            self.errors.record(f"GET {path}: {type(e).__name__}")
            raise ExchangeError(f"network error on {path}: {type(e).__name__}") from e
        if r.status_code in (418, 429):
            self.errors.record(f"GET {path}: {r.status_code}")
            raise RateLimited(f"rate limited ({r.status_code})", http_status=r.status_code)
        if r.status_code >= 400:
            self.errors.record(f"GET {path}: {r.status_code}")
            raise ExchangeError(f"HTTP {r.status_code} on {path}: {r.text[:200]}", http_status=r.status_code)
        return r.json()

    async def server_time(self) -> int:
        return int((await self._get("/fapi/v1/time"))["serverTime"])

    async def exchange_info(self) -> dict[str, Any]:
        return await self._get("/fapi/v1/exchangeInfo")

    async def filters(self, symbol: str) -> SymbolFilters:
        info = await self.exchange_info()
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                return parse_filters(s)
        raise ExchangeError(f"symbol {symbol} not found in exchangeInfo")

    async def klines(self, symbol: str, interval: str, limit: int = 500,
                     start_ms: int | None = None, end_ms: int | None = None) -> list[list[Any]]:
        params: dict[str, Any] = {"symbol": symbol, "interval": interval, "limit": min(limit, 1500)}
        if start_ms is not None:
            params["startTime"] = start_ms
        if end_ms is not None:
            params["endTime"] = end_ms
        return await self._get("/fapi/v1/klines", params)

    async def premium_index(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/premiumIndex", {"symbol": symbol})

    async def open_interest(self, symbol: str) -> float:
        return _f((await self._get("/fapi/v1/openInterest", {"symbol": symbol}))["openInterest"])

    async def open_interest_hist(self, symbol: str, period: str = "5m", limit: int = 30) -> list[dict[str, Any]]:
        return await self._get("/futures/data/openInterestHist", {"symbol": symbol, "period": period, "limit": limit})

    async def ticker_24h(self, symbol: str) -> dict[str, Any]:
        return await self._get("/fapi/v1/ticker/24hr", {"symbol": symbol})

    async def book_ticker(self, symbol: str) -> BookTicker:
        d = await self._get("/fapi/v1/ticker/bookTicker", {"symbol": symbol})
        return BookTicker(bid=_f(d["bidPrice"]), ask=_f(d["askPrice"]), ts=int(d.get("time", 0)))

    async def funding_history(self, symbol: str, start_ms: int, end_ms: int | None = None,
                              limit: int = 1000) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"symbol": symbol, "startTime": start_ms, "limit": limit}
        if end_ms:
            params["endTime"] = end_ms
        return await self._get("/fapi/v1/fundingRate", params)


class BinanceFuturesAdapter(ExecutionAdapter):
    """Signed USDⓈ-M futures execution adapter (LIVE mode)."""

    name = "binance"
    mode = "live"

    def __init__(self, api_key: str, api_secret: str, base_url: str, spot_url: str = "https://api.binance.com",
                 recv_window: int = 5000, testnet: bool = False, timeout: float = 10.0,
                 client: httpx.AsyncClient | None = None) -> None:
        if not api_key or not api_secret:
            raise AuthError("Binance API credentials are not configured")
        self._key = api_key
        self._secret = api_secret.encode()
        self.base_url = base_url.rstrip("/")
        self.spot_url = spot_url.rstrip("/")
        self.recv_window = recv_window
        self.testnet = testnet
        self._client = client or httpx.AsyncClient(timeout=timeout, headers={"User-Agent": "kestrel/1.0"})
        self._offset_ms = 0
        self._offset_checked = 0.0
        self._filters: dict[str, tuple[float, SymbolFilters]] = {}
        self._leverage: dict[str, int] = {}
        self.errors = ErrorTracker()
        self.used_weight_1m = 0

    @property
    def key_hint(self) -> str:
        return "…" + self._key[-4:]

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ plumbing
    async def server_time_offset_ms(self) -> int:
        t0 = time.time()
        r = await self._client.get(f"{self.base_url}/fapi/v1/time")
        t1 = time.time()
        server = int(r.json()["serverTime"])
        local_mid = int((t0 + t1) / 2 * 1000)
        self._offset_ms = server - local_mid
        self._offset_checked = t1
        return self._offset_ms

    def _ts(self) -> int:
        return int(time.time() * 1000) + self._offset_ms

    def _sign(self, params: dict[str, Any]) -> str:
        q = urlencode(params)
        sig = hmac.new(self._secret, q.encode(), hashlib.sha256).hexdigest()
        return f"{q}&signature={sig}"

    async def _request(self, method: str, path: str, params: dict[str, Any] | None = None,
                       signed: bool = True, base: str | None = None, order_path: bool = False) -> Any:
        if signed and time.time() - self._offset_checked > 300:
            try:
                await self.server_time_offset_ms()
            except Exception as e:  # noqa: BLE001 - keep the previous offset, retry later
                log.debug("server time sync failed: %s", type(e).__name__)
        params = {k: v for k, v in (params or {}).items() if v is not None}
        if signed:
            params["recvWindow"] = self.recv_window
            params["timestamp"] = self._ts()
            query = self._sign(params)
        else:
            query = urlencode(params)
        url = f"{base or self.base_url}{path}"
        if query:
            url = f"{url}?{query}"
        try:
            r = await self._client.request(method, url, headers={"X-MBX-APIKEY": self._key})
        except httpx.TimeoutException as e:
            self.errors.record(f"{method} {path}: timeout")
            if order_path:
                raise UnknownOrderStatus(f"timeout on {method} {path}; order status unknown") from e
            raise ExchangeError(f"timeout on {method} {path}") from e
        except httpx.HTTPError as e:
            self.errors.record(f"{method} {path}: {type(e).__name__}")
            # Connection never established → safe to treat as not sent, except ambiguous write errors.
            if order_path and not isinstance(e, httpx.ConnectError):
                raise UnknownOrderStatus(f"{type(e).__name__} on {method} {path}; order status unknown") from e
            raise ExchangeError(f"network error on {method} {path}: {type(e).__name__}") from e

        w = r.headers.get("X-MBX-USED-WEIGHT-1M") or r.headers.get("x-mbx-used-weight-1m")
        if w and w.isdigit():
            self.used_weight_1m = int(w)
        try:
            body = r.json()
        except ValueError:
            body = {"msg": r.text[:300]}

        if r.status_code in (418, 429):
            self.errors.record(f"{method} {path}: {r.status_code}")
            raise RateLimited(f"Binance rate limit ({r.status_code})", http_status=r.status_code)
        if r.status_code >= 500:
            self.errors.record(f"{method} {path}: {r.status_code}")
            if order_path:
                raise UnknownOrderStatus(f"HTTP {r.status_code} on {path}: execution status unknown",
                                         http_status=r.status_code)
            raise ExchangeError(f"HTTP {r.status_code} on {path}", http_status=r.status_code)
        if r.status_code >= 400 or (isinstance(body, dict) and isinstance(body.get("code"), int) and body["code"] < 0):
            code = body.get("code") if isinstance(body, dict) else None
            msg = body.get("msg", "") if isinstance(body, dict) else str(body)
            if code in NOT_FOUND_CODES:
                raise OrderRejected(f"{msg}", code=code, http_status=r.status_code)
            self.errors.record(f"{method} {path}: {code}")
            if code == -1021:  # timestamp outside recvWindow → resync next time
                self._offset_checked = 0
            if code in UNKNOWN_STATUS_CODES and order_path:
                raise UnknownOrderStatus(f"{code} {msg}", code=code, http_status=r.status_code)
            if code in (-2014, -2015, -1022) or r.status_code == 401:
                raise AuthError(f"Binance rejected credentials ({code}): {msg}", code=code, http_status=r.status_code)
            if order_path:
                raise OrderRejected(f"Binance rejected order ({code}): {msg}", code=code, http_status=r.status_code)
            raise ExchangeError(f"Binance error ({code}): {msg}", code=code, http_status=r.status_code)
        return body

    # ------------------------------------------------------------------ info
    async def get_filters(self, symbol: str) -> SymbolFilters:
        cached = self._filters.get(symbol)
        if cached and time.time() - cached[0] < 3600:
            return cached[1]
        info = await self._request("GET", "/fapi/v1/exchangeInfo", signed=False)
        for s in info.get("symbols", []):
            if s.get("symbol") == symbol:
                f = parse_filters(s)
                self._filters[symbol] = (time.time(), f)
                return f
        raise ExchangeError(f"symbol {symbol} not in exchangeInfo")

    async def api_restrictions(self) -> dict[str, Any]:
        """Key permissions from the spot SAPI (not available on testnet)."""
        return await self._request("GET", "/sapi/v1/account/apiRestrictions", base=self.spot_url)

    async def position_mode_dual(self) -> bool:
        d = await self._request("GET", "/fapi/v1/positionSide/dual")
        return bool(d.get("dualSidePosition"))

    async def multi_assets_mode(self) -> bool:
        d = await self._request("GET", "/fapi/v1/multiAssetsMargin")
        return bool(d.get("multiAssetsMargin"))

    async def symbol_config(self, symbol: str) -> dict[str, Any]:
        d = await self._request("GET", "/fapi/v1/symbolConfig", {"symbol": symbol})
        if isinstance(d, list):
            for row in d:
                if row.get("symbol") == symbol:
                    return row
            return {}
        return d

    async def get_account(self) -> AccountInfo:
        d = await self._request("GET", "/fapi/v3/account")
        return AccountInfo(
            equity=_f(d.get("totalMarginBalance")),
            wallet_balance=_f(d.get("totalWalletBalance")),
            available_balance=_f(d.get("availableBalance")),
            unrealized_pnl=_f(d.get("totalUnrealizedProfit")),
            raw={k: d.get(k) for k in ("totalMarginBalance", "totalWalletBalance", "availableBalance")},
        )

    async def get_position(self, symbol: str) -> PositionInfo | None:
        rows = await self._request("GET", "/fapi/v3/positionRisk", {"symbol": symbol})
        for p in rows or []:
            if p.get("symbol") != symbol:
                continue
            amt = _f(p.get("positionAmt"))
            if amt == 0:
                continue
            liq = _f(p.get("liquidationPrice"))
            lev = self._leverage.get(symbol)
            if lev is None:
                try:
                    lev = int(_f((await self.symbol_config(symbol)).get("leverage"), 0)) or None
                    if lev:
                        self._leverage[symbol] = lev
                except ExchangeError:
                    lev = None
            return PositionInfo(
                symbol=symbol,
                quantity=amt,
                entry_price=_f(p.get("entryPrice")),
                mark_price=_f(p.get("markPrice")),
                unrealized_pnl=_f(p.get("unRealizedProfit")),
                liquidation_price=liq if liq > 0 else None,
                leverage=lev,
                margin=_f(p.get("isolatedMargin")) or _f(p.get("initialMargin")) or None,
                raw={k: p.get(k) for k in ("positionAmt", "entryPrice", "markPrice", "liquidationPrice", "notional")},
            )
        return None

    @staticmethod
    def _order_from_regular(o: dict[str, Any]) -> OrderInfo:
        return OrderInfo(
            symbol=o.get("symbol", ""),
            client_order_id=o.get("clientOrderId", ""),
            venue_order_id=str(o.get("orderId", "")),
            side=o.get("side", ""),
            type=o.get("type", o.get("origType", "")),
            status=o.get("status", "UNKNOWN"),
            quantity=_f(o.get("origQty")),
            filled_qty=_f(o.get("executedQty")),
            avg_price=_f(o.get("avgPrice")) or None,
            price=_f(o.get("price")) or None,
            trigger_price=_f(o.get("stopPrice")) or None,
            reduce_only=bool(o.get("reduceOnly")),
            close_position=bool(o.get("closePosition")),
            conditional=False,
            update_time=int(o.get("updateTime", 0) or 0),
            raw=o,
        )

    @staticmethod
    def _order_from_algo(o: dict[str, Any]) -> OrderInfo:
        status = o.get("algoStatus", "UNKNOWN")
        return OrderInfo(
            symbol=o.get("symbol", ""),
            client_order_id=o.get("clientAlgoId", ""),
            venue_order_id=str(o.get("algoId", "")),
            side=o.get("side", ""),
            type=o.get("orderType", o.get("type", "")),
            status=status,
            quantity=_f(o.get("quantity")),
            filled_qty=_f(o.get("actualQty")) if status in ("FILLED", "TRIGGERED", "FINISHED") else 0.0,
            avg_price=_f(o.get("actualPrice")) or None,
            price=_f(o.get("price")) or None,
            trigger_price=_f(o.get("triggerPrice")) or None,
            reduce_only=str(o.get("reduceOnly")).lower() == "true",
            close_position=str(o.get("closePosition")).lower() == "true",
            conditional=True,
            update_time=int(o.get("updateTime", 0) or 0),
            raw=o,
        )

    async def get_open_orders(self, symbol: str) -> list[OrderInfo]:
        regular, algo = await asyncio.gather(
            self._request("GET", "/fapi/v1/openOrders", {"symbol": symbol}),
            self._request("GET", "/fapi/v1/openAlgoOrders", {"symbol": symbol}),
        )
        out = [self._order_from_regular(o) for o in (regular or [])]
        algo_rows = algo.get("orders", algo.get("rows", [])) if isinstance(algo, dict) else (algo or [])
        out += [self._order_from_algo(o) for o in algo_rows if o.get("symbol", symbol) == symbol]
        return out

    async def get_order(self, symbol: str, client_order_id: str, conditional: bool) -> OrderInfo | None:
        try:
            if conditional:
                d = await self._request("GET", "/fapi/v1/algoOrder", {"clientAlgoId": client_order_id})
                return self._order_from_algo(d)
            d = await self._request("GET", "/fapi/v1/order", {"symbol": symbol, "origClientOrderId": client_order_id})
            return self._order_from_regular(d)
        except OrderRejected as e:
            if e.code in NOT_FOUND_CODES:
                return None
            raise

    async def get_mark_price(self, symbol: str) -> float:
        d = await self._request("GET", "/fapi/v1/premiumIndex", {"symbol": symbol}, signed=False)
        return _f(d["markPrice"])

    async def get_book_ticker(self, symbol: str) -> BookTicker | None:
        d = await self._request("GET", "/fapi/v1/ticker/bookTicker", {"symbol": symbol}, signed=False)
        return BookTicker(bid=_f(d["bidPrice"]), ask=_f(d["askPrice"]), ts=int(d.get("time", 0)))

    # ------------------------------------------------------------------ account config
    async def set_leverage(self, symbol: str, leverage: int) -> int:
        d = await self._request("POST", "/fapi/v1/leverage", {"symbol": symbol, "leverage": int(leverage)})
        lev = int(d.get("leverage", leverage))
        self._leverage[symbol] = lev
        return lev

    async def ensure_isolated_margin(self, symbol: str) -> None:
        cfg = await self.symbol_config(symbol)
        if str(cfg.get("marginType", "")).upper() == "ISOLATED":
            return
        try:
            await self._request("POST", "/fapi/v1/marginType", {"symbol": symbol, "marginType": "ISOLATED"})
        except ExchangeError as e:
            if e.code == -4046:  # "No need to change margin type."
                return
            raise

    # ------------------------------------------------------------------ orders
    async def place_market_order(self, symbol: str, side: str, quantity: float, client_order_id: str,
                                 reduce_only: bool = False) -> OrderInfo:
        f = await self.get_filters(symbol)
        params = {
            "symbol": symbol,
            "side": side,
            "type": "MARKET",
            "quantity": f.fmt_qty(quantity, market=True),
            "newClientOrderId": client_order_id,
            "newOrderRespType": "RESULT",
        }
        if reduce_only:
            params["reduceOnly"] = "true"
        d = await self._request("POST", "/fapi/v1/order", params, order_path=True)
        return self._order_from_regular(d)

    async def _place_conditional(self, symbol: str, side: str, order_type: str, trigger_price: float,
                                 quantity: float, client_order_id: str) -> OrderInfo:
        f = await self.get_filters(symbol)
        params = {
            "algoType": "CONDITIONAL",
            "symbol": symbol,
            "side": side,
            "type": order_type,
            "triggerPrice": f.fmt_price(trigger_price),
            "quantity": f.fmt_qty(quantity, market=True),
            "reduceOnly": "true",
            "workingType": "MARK_PRICE",
            "priceProtect": "true",
            "clientAlgoId": client_order_id,
        }
        d = await self._request("POST", "/fapi/v1/algoOrder", params, order_path=True)
        return self._order_from_algo(d)

    async def place_stop_market(self, symbol: str, side: str, trigger_price: float, quantity: float,
                                client_order_id: str) -> OrderInfo:
        return await self._place_conditional(symbol, side, "STOP_MARKET", trigger_price, quantity, client_order_id)

    async def place_take_profit_market(self, symbol: str, side: str, trigger_price: float, quantity: float,
                                       client_order_id: str) -> OrderInfo:
        return await self._place_conditional(symbol, side, "TAKE_PROFIT_MARKET", trigger_price, quantity,
                                             client_order_id)

    async def cancel_order(self, symbol: str, client_order_id: str, conditional: bool) -> bool:
        try:
            if conditional:
                await self._request("DELETE", "/fapi/v1/algoOrder", {"clientAlgoId": client_order_id}, order_path=True)
            else:
                await self._request("DELETE", "/fapi/v1/order",
                                    {"symbol": symbol, "origClientOrderId": client_order_id}, order_path=True)
            return True
        except OrderRejected as e:
            if e.code in NOT_FOUND_CODES:
                return False
            raise

    async def cancel_all_orders(self, symbol: str) -> None:
        for path in ("/fapi/v1/allOpenOrders", "/fapi/v1/algoOpenOrders"):
            try:
                await self._request("DELETE", path, {"symbol": symbol}, order_path=True)
            except OrderRejected as e:
                if e.code not in NOT_FOUND_CODES:
                    raise

    async def get_fills(self, symbol: str, start_ms: int) -> list[Fill]:
        rows = await self._request("GET", "/fapi/v1/userTrades", {"symbol": symbol, "startTime": start_ms, "limit": 1000})
        return [
            Fill(order_id=str(r.get("orderId")), side=r.get("side", ""), price=_f(r.get("price")), qty=_f(r.get("qty")),
                 commission=_f(r.get("commission")), realized_pnl=_f(r.get("realizedPnl")), time=int(r.get("time", 0)),
                 maker=bool(r.get("maker")))
            for r in rows or []
        ]

    async def get_funding_since(self, symbol: str, start_ms: int) -> float:
        rows = await self._request("GET", "/fapi/v1/income",
                                   {"symbol": symbol, "incomeType": "FUNDING_FEE", "startTime": start_ms, "limit": 1000})
        return sum(_f(r.get("income")) for r in rows or [])
