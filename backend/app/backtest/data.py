"""Historical data for backtests: cached in the ``candles`` / ``funding_rates`` tables."""

from __future__ import annotations

import asyncio
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from app.db import models as M
from app.exchange.binance_rest import BinancePublic
from app.market.data_service import rest_row, upsert_candles
from app.market.features import INTERVAL_MS


async def load_klines(public: BinancePublic, factory, symbol: str, tf: str, start_ms: int, end_ms: int  # noqa: ANN001
                      ) -> list[list[Any]]:
    iv = INTERVAL_MS[tf]
    start_ms = start_ms - start_ms % iv
    expected = (end_ms - start_ms) // iv
    async with factory() as s:
        rows = (await s.execute(select(M.Candle).where(M.Candle.symbol == symbol, M.Candle.interval == tf,
                                                       M.Candle.open_time >= start_ms, M.Candle.open_time < end_ms)
                                .order_by(M.Candle.open_time))).scalars().all()
    if len(rows) < expected * 0.995:
        cursor = start_ms
        fetched: list[dict[str, Any]] = []
        while cursor < end_ms:
            batch = await public.klines(symbol, tf, limit=1500, start_ms=cursor, end_ms=end_ms - 1)
            if not batch:
                break
            fetched += [rest_row(b) for b in batch]
            cursor = int(batch[-1][0]) + iv
            await asyncio.sleep(0.15)  # stay far below the weight limit
        import time

        now_ms = int(time.time() * 1000)
        fetched = [r for r in fetched if r["open_time"] + iv <= now_ms]
        await upsert_candles(factory, symbol, tf, fetched)
        return [[r["open_time"], r["open"], r["high"], r["low"], r["close"], r["volume"]] for r in fetched]
    return [[r.open_time, r.open, r.high, r.low, r.close, r.volume] for r in rows]


async def load_funding(public: BinancePublic, factory, symbol: str, start_ms: int, end_ms: int  # noqa: ANN001
                       ) -> list[tuple[int, float]]:
    async with factory() as s:
        rows = (await s.execute(select(M.FundingRate).where(M.FundingRate.symbol == symbol,
                                                            M.FundingRate.funding_time >= start_ms,
                                                            M.FundingRate.funding_time <= end_ms)
                                .order_by(M.FundingRate.funding_time))).scalars().all()
    expected = (end_ms - start_ms) // (8 * 3_600_000)
    if len(rows) >= expected * 0.95 and rows:
        return [(r.funding_time, r.rate) for r in rows]
    out: list[tuple[int, float]] = []
    cursor = start_ms
    while cursor < end_ms:
        batch = await public.funding_history(symbol, cursor, end_ms, 1000)
        if not batch:
            break
        out += [(int(b["fundingTime"]), float(b["fundingRate"])) for b in batch]
        cursor = int(batch[-1]["fundingTime"]) + 1
        if len(batch) < 1000:
            break
        await asyncio.sleep(0.15)
    if out:
        async with factory() as s:
            dialect = s.bind.dialect.name
            ins = pg_insert if dialect == "postgresql" else sqlite_insert
            stmt = ins(M.FundingRate).values([{"symbol": symbol, "funding_time": t, "rate": r} for t, r in out])
            await s.execute(stmt.on_conflict_do_nothing(index_elements=["symbol", "funding_time"]))
            await s.commit()
    return out
