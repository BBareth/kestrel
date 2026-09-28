"""Live market data: REST backfill + WebSocket streams + REST fallback.

Streams (2026 URL layout — legacy /ws and /stream roots were retired 2026-04-23):
* ``wss://fstream.binance.com/market/stream?streams=`` klines (1m/5m/15m/1h/4h),
  ``markPrice@1s`` (mark, index, funding rate, next funding time), ``ticker`` (24h),
  ``forceOrder`` (liquidations)
* ``wss://fstream.binance.com/public/stream?streams=`` ``bookTicker`` (spread)

Implements ``PriceSource`` for the paper exchange. Freshness of every input is
tracked so the safety supervisor can pause or halt trading on stale data.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import websockets
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db import models as M
from app.exchange.binance_rest import BinancePublic
from app.exchange.models import BookTicker, ExchangeError
from app.market.features import INTERVAL_MS, TIMEFRAMES, Series

log = logging.getLogger("kestrel.market")

BUFFER = {"1m": 600, "5m": 600, "15m": 400, "1h": 400, "4h": 400}

KlineHook = Callable[[str, dict[str, Any]], Awaitable[None]]


def kline_row(k: dict[str, Any]) -> dict[str, Any]:
    return {
        "open_time": int(k["t"]), "open": float(k["o"]), "high": float(k["h"]), "low": float(k["l"]),
        "close": float(k["c"]), "volume": float(k["v"]), "quote_volume": float(k.get("q", 0)),
        "trades": int(k.get("n", 0)), "taker_buy_volume": float(k.get("V", 0)),
    }


def rest_row(r: list[Any]) -> dict[str, Any]:
    return {
        "open_time": int(r[0]), "open": float(r[1]), "high": float(r[2]), "low": float(r[3]),
        "close": float(r[4]), "volume": float(r[5]), "quote_volume": float(r[7]), "trades": int(r[8]),
        "taker_buy_volume": float(r[9]),
    }


async def upsert_candles(factory, symbol: str, interval: str, rows: list[dict[str, Any]]) -> None:  # noqa: ANN001
    if not rows:
        return
    async with factory() as s:
        dialect = s.bind.dialect.name
        ins = pg_insert if dialect == "postgresql" else sqlite_insert
        for i in range(0, len(rows), 500):
            chunk = [{"symbol": symbol, "interval": interval, **r} for r in rows[i:i + 500]]
            stmt = ins(M.Candle).values(chunk)
            stmt = stmt.on_conflict_do_update(
                index_elements=["symbol", "interval", "open_time"],
                set_={c: getattr(stmt.excluded, c) for c in
                      ("open", "high", "low", "close", "volume", "quote_volume", "trades", "taker_buy_volume")})
            await s.execute(stmt)
        await s.commit()


class MarketDataService:
    def __init__(self, symbol: str, public: BinancePublic, ws_market_url: str, ws_public_url: str,
                 session_factory=None) -> None:  # noqa: ANN001
        self.symbol = symbol
        self.sym_l = symbol.lower()
        self.public = public
        self.ws_market_url = ws_market_url.rstrip("/")
        self.ws_public_url = ws_public_url.rstrip("/")
        self.factory = session_factory
        self.closed: dict[str, list[dict[str, Any]]] = {tf: [] for tf in TIMEFRAMES}
        self.forming: dict[str, dict[str, Any] | None] = {tf: None for tf in TIMEFRAMES}
        self._mark: float | None = None
        self._index: float | None = None
        self._last: float | None = None
        self._book: BookTicker | None = None
        self.funding_rate: float | None = None
        self.next_funding_ms: int | None = None
        self.ticker24: dict[str, Any] = {}
        self.open_interest: float | None = None
        self.oi_hist: list[tuple[int, float]] = []
        self.liqs: deque[tuple[float, str, float]] = deque(maxlen=5000)
        self.last_msg_at = 0.0
        self.last_mark_at = 0.0
        self.last_book_at = 0.0
        self.last_kline_at: dict[str, float] = {tf: 0.0 for tf in TIMEFRAMES}
        self.ws_connected = False
        self.ws_public_connected = False
        self.reconnects = 0
        self.errors: deque[float] = deque(maxlen=200)
        self._hooks: list[KlineHook] = []
        self._tasks: list[asyncio.Task[Any]] = []
        self._stop = asyncio.Event()
        self._persist_q: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()

    # ------------------------------------------------------------------ PriceSource
    def mark_price(self) -> float | None:
        return self._mark

    def last_price(self) -> float | None:
        return self._last if self._last is not None else self._mark

    def book(self) -> BookTicker | None:
        return self._book

    # ------------------------------------------------------------------ public API
    def on_kline_closed(self, hook: KlineHook) -> None:
        self._hooks.append(hook)

    def series(self, tf: str, include_forming: bool = False) -> Series:
        rows = list(self.closed[tf])
        if include_forming and self.forming[tf]:
            rows.append(self.forming[tf])
        return Series.from_rows(tf, rows)

    def data_age(self) -> float:
        """Seconds since the freshest price input (mark price or 1m kline)."""
        latest = max(self.last_mark_at, self.last_kline_at.get("1m", 0.0))
        return time.time() - latest if latest else float("inf")

    def spread_bps(self) -> float | None:
        if self._book is None or time.time() - self.last_book_at > 30:
            return None
        return self._book.spread_bps

    def oi_change_1h_pct(self) -> float | None:
        if len(self.oi_hist) < 13:
            return None
        now_v = self.oi_hist[-1][1]
        prev = self.oi_hist[-13][1]
        return (now_v - prev) / prev * 100 if prev else None

    def liquidations_1h(self) -> dict[str, float]:
        cutoff = time.time() - 3600
        longs = sum(q for t, side, q in self.liqs if t >= cutoff and side == "SELL")
        shorts = sum(q for t, side, q in self.liqs if t >= cutoff and side == "BUY")
        return {"long_liquidations_usdt": round(longs, 0), "short_liquidations_usdt": round(shorts, 0)}

    def status(self) -> dict[str, Any]:
        now = time.time()
        return {
            "ws_connected": self.ws_connected,
            "ws_public_connected": self.ws_public_connected,
            "data_age_s": round(self.data_age(), 1) if self.data_age() != float("inf") else None,
            "last_msg_age_s": round(now - self.last_msg_at, 1) if self.last_msg_at else None,
            "reconnects": self.reconnects,
            "kline_age_s": {tf: round(now - t, 1) if t else None for tf, t in self.last_kline_at.items()},
            "bars": {tf: len(v) for tf, v in self.closed.items()},
        }

    def snapshot(self) -> dict[str, Any]:
        t = self.ticker24
        return {
            "symbol": self.symbol,
            "price": self.last_price(),
            "mark_price": self._mark,
            "index_price": self._index,
            "bid": self._book.bid if self._book else None,
            "ask": self._book.ask if self._book else None,
            "spread_bps": self.spread_bps(),
            "funding_rate": self.funding_rate,
            "next_funding_ms": self.next_funding_ms,
            "change_24h_pct": float(t["P"]) if t.get("P") is not None else None,
            "high_24h": float(t["h"]) if t.get("h") else None,
            "low_24h": float(t["l"]) if t.get("l") else None,
            "volume_24h": float(t["v"]) if t.get("v") else None,
            "quote_volume_24h": float(t["q"]) if t.get("q") else None,
            "open_interest": self.open_interest,
            "oi_change_1h_pct": self.oi_change_1h_pct(),
            "liquidations_1h": self.liquidations_1h(),
            "ts": int(time.time() * 1000),
        }

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        await self.backfill()
        self._tasks = [
            asyncio.create_task(self._run_ws(self._market_url(), self._on_market_msg, "market"), name="ws-market"),
            asyncio.create_task(self._run_ws(self._public_url(), self._on_public_msg, "public"), name="ws-public"),
            asyncio.create_task(self._rest_poller(), name="rest-poller"),
            asyncio.create_task(self._persist_worker(), name="candle-persist"),
        ]

    async def stop(self) -> None:
        self._stop.set()
        for t in self._tasks:
            t.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    def _market_url(self) -> str:
        streams = [f"{self.sym_l}@kline_{tf}" for tf in TIMEFRAMES] + [
            f"{self.sym_l}@markPrice@1s", f"{self.sym_l}@ticker", f"{self.sym_l}@forceOrder"]
        return f"{self.ws_market_url}/stream?streams={'/'.join(streams)}"

    def _public_url(self) -> str:
        return f"{self.ws_public_url}/stream?streams={self.sym_l}@bookTicker"

    async def backfill(self) -> None:
        for tf in TIMEFRAMES:
            rows = await self.public.klines(self.symbol, tf, limit=BUFFER[tf] + 1)
            now_ms = int(time.time() * 1000)
            parsed = [rest_row(r) for r in rows]
            closed = [r for r in parsed if r["open_time"] + INTERVAL_MS[tf] <= now_ms]
            forming = [r for r in parsed if r["open_time"] + INTERVAL_MS[tf] > now_ms]
            self.closed[tf] = closed[-BUFFER[tf]:]
            self.forming[tf] = forming[-1] if forming else None
            self.last_kline_at[tf] = time.time()
            if self.factory:
                await upsert_candles(self.factory, self.symbol, tf, closed)
        pi = await self.public.premium_index(self.symbol)
        self._apply_premium(pi)
        try:
            self._book = await self.public.book_ticker(self.symbol)
            self.last_book_at = time.time()
            self.ticker24 = {k: v for k, v in (await self.public.ticker_24h(self.symbol)).items()}
            self.ticker24 = {"P": self.ticker24.get("priceChangePercent"), "h": self.ticker24.get("highPrice"),
                             "l": self.ticker24.get("lowPrice"), "v": self.ticker24.get("volume"),
                             "q": self.ticker24.get("quoteVolume"), "c": self.ticker24.get("lastPrice")}
            await self._refresh_oi(full=True)
        except ExchangeError as e:
            log.warning("partial backfill: %s", e)
        self._last = self.closed["1m"][-1]["close"] if self.closed["1m"] else self._mark
        log.info("market backfill complete", extra={"bars": {tf: len(v) for tf, v in self.closed.items()}})

    def _apply_premium(self, pi: dict[str, Any]) -> None:
        self._mark = float(pi["markPrice"])
        self._index = float(pi.get("indexPrice") or 0) or None
        self.funding_rate = float(pi.get("lastFundingRate") or 0)
        self.next_funding_ms = int(pi.get("nextFundingTime") or 0) or None
        self.last_mark_at = time.time()

    async def ensure_fresh(self, tf: str) -> bool:
        """Make sure the last *closed* bar of ``tf`` is present (REST patch if the WS lagged)."""
        now_ms = int(time.time() * 1000)
        iv = INTERVAL_MS[tf]
        expected_open = (now_ms // iv) * iv - iv
        buf = self.closed[tf]
        if buf and buf[-1]["open_time"] >= expected_open:
            return True
        try:
            rows = await self.public.klines(self.symbol, tf, limit=5)
        except ExchangeError as e:
            log.warning("ensure_fresh %s failed: %s", tf, e)
            return False
        for r in (rest_row(x) for x in rows):
            if r["open_time"] + iv <= now_ms:
                self._merge_closed(tf, r)
        return bool(self.closed[tf]) and self.closed[tf][-1]["open_time"] >= expected_open

    def _merge_closed(self, tf: str, row: dict[str, Any]) -> bool:
        buf = self.closed[tf]
        if buf and row["open_time"] <= buf[-1]["open_time"]:
            for i in range(len(buf) - 1, max(-1, len(buf) - 5), -1):
                if buf[i]["open_time"] == row["open_time"]:
                    buf[i] = row
                    return False
            return False
        if buf and row["open_time"] - buf[-1]["open_time"] > INTERVAL_MS[tf]:
            log.warning("kline gap detected", extra={"tf": tf, "gap_bars": (row["open_time"] - buf[-1]["open_time"]) // INTERVAL_MS[tf] - 1})
            asyncio.get_event_loop().create_task(self._fill_gap(tf, buf[-1]["open_time"], row["open_time"]))
        buf.append(row)
        if len(buf) > BUFFER[tf]:
            del buf[: len(buf) - BUFFER[tf]]
        return True

    async def _fill_gap(self, tf: str, after_ms: int, before_ms: int) -> None:
        try:
            rows = await self.public.klines(self.symbol, tf, limit=1500, start_ms=after_ms + 1, end_ms=before_ms - 1)
        except ExchangeError:
            return
        missing = [rest_row(r) for r in rows]
        if not missing:
            return
        merged = {r["open_time"]: r for r in self.closed[tf]}
        for r in missing:
            merged.setdefault(r["open_time"], r)
        self.closed[tf] = [merged[k] for k in sorted(merged)][-BUFFER[tf]:]
        if self.factory:
            await upsert_candles(self.factory, self.symbol, tf, missing)

    # ------------------------------------------------------------------ websocket
    async def _run_ws(self, url: str, handler: Callable[[dict[str, Any]], Awaitable[None]], name: str) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(url, ping_interval=30, ping_timeout=30, close_timeout=5,
                                              max_size=2 ** 22) as ws:
                    if name == "market":
                        self.ws_connected = True
                    else:
                        self.ws_public_connected = True
                    log.info("websocket connected", extra={"stream": name})
                    backoff = 1.0
                    while not self._stop.is_set():
                        raw = await asyncio.wait_for(ws.recv(), timeout=20)
                        self.last_msg_at = time.time()
                        msg = json.loads(raw)
                        data = msg.get("data", msg)
                        try:
                            await handler(data)
                        except Exception:  # noqa: BLE001 - a bad message must not kill the stream
                            log.exception("stream handler error")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.errors.append(time.time())
                log.warning("websocket %s disconnected: %s", name, type(e).__name__)
            finally:
                if name == "market":
                    self.ws_connected = False
                else:
                    self.ws_public_connected = False
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
            if name == "market":
                # catch up on anything missed while disconnected
                for tf in TIMEFRAMES:
                    await self.ensure_fresh(tf)

    async def _on_market_msg(self, d: dict[str, Any]) -> None:
        e = d.get("e")
        if e == "kline":
            k = d["k"]
            tf = k["i"]
            if tf not in self.closed:
                return
            row = kline_row(k)
            self.last_kline_at[tf] = time.time()
            if tf == "1m":
                self._last = row["close"]
            if k.get("x"):
                self.forming[tf] = None
                if self._merge_closed(tf, row):
                    await self._persist_q.put((tf, row))
                    for hook in self._hooks:
                        try:
                            await hook(tf, row)
                        except Exception:  # noqa: BLE001
                            log.exception("kline hook failed")
            else:
                self.forming[tf] = row
        elif e == "markPriceUpdate":
            self._mark = float(d["p"])
            self._index = float(d.get("i") or 0) or self._index
            if d.get("r") not in (None, ""):
                self.funding_rate = float(d["r"])
            if d.get("T"):
                self.next_funding_ms = int(d["T"])
            self.last_mark_at = time.time()
        elif e == "24hrTicker":
            self.ticker24 = {k: d.get(k) for k in ("P", "h", "l", "v", "q", "c")}
        elif e == "forceOrder":
            o = d.get("o", {})
            try:
                self.liqs.append((time.time(), o.get("S", ""), float(o.get("q", 0)) * float(o.get("ap") or o.get("p") or 0)))
            except (TypeError, ValueError):
                pass

    async def _on_public_msg(self, d: dict[str, Any]) -> None:
        if d.get("e") == "bookTicker" or ("b" in d and "a" in d):
            self._book = BookTicker(bid=float(d["b"]), ask=float(d["a"]), ts=int(d.get("T") or d.get("E") or 0))
            self.last_book_at = time.time()

    # ------------------------------------------------------------------ REST
    async def _refresh_oi(self, full: bool = False) -> None:
        self.open_interest = await self.public.open_interest(self.symbol)
        if full or not self.oi_hist or time.time() * 1000 - self.oi_hist[-1][0] > 300_000:
            hist = await self.public.open_interest_hist(self.symbol, "5m", 30)
            self.oi_hist = [(int(h["timestamp"]), float(h["sumOpenInterest"])) for h in hist]

    async def _rest_poller(self) -> None:
        """Open interest every 60s; mark/book fallback whenever the streams go quiet."""
        last_oi = 0.0
        while not self._stop.is_set():
            try:
                now = time.time()
                if now - last_oi > 60:
                    await self._refresh_oi()
                    last_oi = now
                if now - self.last_mark_at > 5:
                    self._apply_premium(await self.public.premium_index(self.symbol))
                if now - self.last_book_at > 10:
                    self._book = await self.public.book_ticker(self.symbol)
                    self.last_book_at = time.time()
                if now - self.last_kline_at.get("1m", 0) > 90:
                    for tf in TIMEFRAMES:
                        await self.ensure_fresh(tf)
                    self.last_kline_at["1m"] = time.time() if self.closed["1m"] else 0
                    if self.closed["1m"]:
                        self._last = self.closed["1m"][-1]["close"]
            except Exception as e:  # noqa: BLE001
                self.errors.append(time.time())
                log.warning("REST poll failed: %s", e)
            await asyncio.sleep(3)

    async def _persist_worker(self) -> None:
        while not self._stop.is_set():
            tf, row = await self._persist_q.get()
            if self.factory is None:
                continue
            try:
                await upsert_candles(self.factory, self.symbol, tf, [row])
            except Exception:  # noqa: BLE001
                log.exception("candle persist failed")
