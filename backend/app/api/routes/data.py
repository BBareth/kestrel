"""Read-only views: dashboard, market, signals, trades, orders, positions, history."""

from __future__ import annotations

from datetime import timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select

from app.api.deps import AppContext, Auth, ctx, current_auth
from app.api.serialize import to_dict
from app.db import models as M
from app.db.types import utcnow
from app.market.features import INTERVAL_MS

router = APIRouter(prefix="/api", tags=["data"])

Mode = Literal["paper", "live"]


@router.get("/dashboard")
async def dashboard(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    st = await c.store.trading_state()
    mode = st["mode"]
    live = await c.store.live_state(["ticker", "evaluation", "engine", "accounts"])
    now = utcnow()
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    async with c.store.factory() as s:
        pos = (await s.execute(select(M.Position).where(M.Position.mode == mode))).scalars().first()
        open_trade = (await s.execute(select(M.Trade).where(M.Trade.mode == mode,
                                                            M.Trade.status.in_(("pending", "open", "closing")))
                                      .order_by(desc(M.Trade.id)).limit(1))).scalar_one_or_none()
        today = (await s.execute(select(M.Trade).where(M.Trade.mode == mode, M.Trade.status == "closed",
                                                       M.Trade.closed_at >= day0))).scalars().all()
        n_closed = (await s.execute(select(func.count()).select_from(M.Trade).where(
            M.Trade.mode == mode, M.Trade.status == "closed"))).scalar() or 0
        n_wins = (await s.execute(select(func.count()).select_from(M.Trade).where(
            M.Trade.mode == mode, M.Trade.status == "closed", M.Trade.realized_pnl > 0))).scalar() or 0
        opened_today = (await s.execute(select(func.count()).select_from(M.Trade).where(
            M.Trade.mode == mode, M.Trade.opened_at >= day0))).scalar() or 0
        last_signal = (await s.execute(select(M.Signal).order_by(desc(M.Signal.id)).limit(1))).scalar_one_or_none()
        last_trade = (await s.execute(select(M.Trade).where(M.Trade.status == "closed")
                                      .order_by(desc(M.Trade.closed_at)).limit(1))).scalar_one_or_none()
    accounts = live.get("accounts", {})
    acct = accounts.get(mode) if isinstance(accounts, dict) else None
    return {
        "mode": mode,
        "trading": st,
        "ticker": live.get("ticker"),
        "evaluation": live.get("evaluation"),
        "engine": live.get("engine"),
        "account": {
            **(acct or {}),
            "today_pnl": round(sum(t.realized_pnl for t in today), 2),
            "today_trades": opened_today,
            "win_rate": round(n_wins / n_closed * 100, 1) if n_closed else None,
            "closed_trades": n_closed,
        },
        "position": to_dict(pos) if pos and pos.quantity else None,
        "trade": to_dict(open_trade, {"snapshot"}) if open_trade else None,
        "last_signal": to_dict(last_signal, {"context", "risk_result"}) if last_signal else None,
        "last_trade": to_dict(last_trade, {"snapshot"}) if last_trade else None,
    }


@router.get("/market/candles")
async def candles(interval: Literal["1m", "5m", "15m", "1h", "4h"] = "5m", limit: int = Query(300, ge=10, le=1500),
                  auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        rows = (await s.execute(select(M.Candle).where(M.Candle.symbol == c.settings.symbol,
                                                       M.Candle.interval == interval)
                                .order_by(desc(M.Candle.open_time)).limit(limit))).scalars().all()
    rows = list(reversed(rows))
    return {"interval": interval, "candles": [[r.open_time // 1000, r.open, r.high, r.low, r.close, r.volume]
                                              for r in rows]}


@router.get("/market/state")
async def market_state(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    live = await c.store.live_state(["ticker", "evaluation"])
    return live


@router.get("/signals")
async def signals(limit: int = Query(100, le=500), status: str | None = None, auth: Auth = Depends(current_auth),
                  c: AppContext = Depends(ctx)) -> list[dict]:
    async with c.store.factory() as s:
        q = select(M.Signal).order_by(desc(M.Signal.id)).limit(limit)
        if status:
            q = q.where(M.Signal.status == status)
        rows = (await s.execute(q)).scalars().all()
    return [to_dict(r, {"context"}) for r in rows]


@router.get("/signals/{sid}")
async def signal_detail(sid: int, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        r = await s.get(M.Signal, sid)
        if r is None:
            raise HTTPException(404, "not found")
        ai = await s.get(M.AIAnalysis, r.ai_analysis_id) if r.ai_analysis_id else None
    return {**to_dict(r), "ai": to_dict(ai, {"request"}) if ai else None}


@router.get("/trades")
async def trades(mode: Mode | None = None, status: str | None = None, limit: int = Query(200, le=1000),
                 auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> list[dict]:
    async with c.store.factory() as s:
        q = select(M.Trade).order_by(desc(M.Trade.id)).limit(limit)
        if mode:
            q = q.where(M.Trade.mode == mode)
        if status:
            q = q.where(M.Trade.status == status)
        rows = (await s.execute(q)).scalars().all()
    return [to_dict(r, {"snapshot"}) for r in rows]


@router.get("/trades/{tid}")
async def trade_detail(tid: int, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        t = await s.get(M.Trade, tid)
        if t is None:
            raise HTTPException(404, "not found")
        ev = (await s.execute(select(M.TradeEvent).where(M.TradeEvent.trade_id == tid).order_by(M.TradeEvent.id))
              ).scalars().all()
        orders = (await s.execute(select(M.Order).where(M.Order.trade_id == tid).order_by(M.Order.id))).scalars().all()
        sig = await s.get(M.Signal, t.signal_id) if t.signal_id else None
        ai = await s.get(M.AIAnalysis, sig.ai_analysis_id) if sig and sig.ai_analysis_id else None
        candles = []
        if t.opened_at:
            start = int((t.opened_at - timedelta(hours=3)).timestamp() * 1000)
            end = int(((t.closed_at or utcnow()) + timedelta(hours=1)).timestamp() * 1000)
            rows = (await s.execute(select(M.Candle).where(M.Candle.symbol == t.symbol, M.Candle.interval == "5m",
                                                           M.Candle.open_time >= start, M.Candle.open_time <= end)
                                    .order_by(M.Candle.open_time))).scalars().all()
            candles = [[r.open_time // 1000, r.open, r.high, r.low, r.close] for r in rows]
    return {"trade": to_dict(t), "events": [to_dict(e) for e in ev], "orders": [to_dict(o, {"raw"}) for o in orders],
            "signal": to_dict(sig, {"context"}) if sig else None, "ai": to_dict(ai, {"request"}) if ai else None,
            "candles": candles}


@router.get("/orders")
async def orders(mode: Mode | None = None, active: bool = False, limit: int = Query(200, le=1000),
                 auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> list[dict]:
    async with c.store.factory() as s:
        q = select(M.Order).order_by(desc(M.Order.id)).limit(limit)
        if mode:
            q = q.where(M.Order.mode == mode)
        if active:
            q = q.where(M.Order.status.in_(("NEW", "PARTIALLY_FILLED", "SUBMITTING")))
        rows = (await s.execute(q)).scalars().all()
    return [to_dict(r, {"raw"}) for r in rows]


@router.get("/positions")
async def positions(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        rows = (await s.execute(select(M.Position))).scalars().all()
        open_trades = (await s.execute(select(M.Trade).where(M.Trade.status.in_(("pending", "open", "closing")))
                                       .order_by(desc(M.Trade.id)))).scalars().all()
    return {"positions": [to_dict(r) for r in rows if r.quantity], "trades": [to_dict(t, {"snapshot"}) for t in open_trades]}


@router.get("/history/events")
async def events(kind: Literal["all", "system", "risk", "audit"] = "all", limit: int = Query(200, le=1000),
                 auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> list[dict]:
    out: list[dict] = []
    async with c.store.factory() as s:
        if kind in ("all", "system"):
            for r in (await s.execute(select(M.SystemEvent).order_by(desc(M.SystemEvent.id)).limit(limit))).scalars():
                out.append({"type": "system", "ts": r.ts.isoformat(), "level": r.level, "title": r.event,
                            "component": r.component, "message": r.message, "resolved": r.resolved})
        if kind in ("all", "risk"):
            for r in (await s.execute(select(M.RiskEvent).order_by(desc(M.RiskEvent.id)).limit(limit))).scalars():
                out.append({"type": "risk", "ts": r.ts.isoformat(), "level": r.severity, "title": r.kind,
                            "message": r.message})
        if kind in ("all", "audit"):
            for r in (await s.execute(select(M.AuditLog).order_by(desc(M.AuditLog.id)).limit(limit))).scalars():
                out.append({"type": "audit", "ts": r.ts.isoformat(), "level": "info", "title": r.action,
                            "message": f"{r.username or 'system'}{' → ' + r.target if r.target else ''}"
                                       f"{' from ' + r.ip if r.ip else ''}", "data": r.data})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out[:limit]


@router.get("/snapshots")
async def snapshots(hours: int = Query(24, le=720), auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> list[dict]:
    async with c.store.factory() as s:
        rows = (await s.execute(select(M.MarketSnapshot).where(M.MarketSnapshot.ts >= utcnow() - timedelta(hours=hours))
                                .order_by(M.MarketSnapshot.ts))).scalars().all()
    return [to_dict(r, {"data"}) for r in rows]


_ = INTERVAL_MS
