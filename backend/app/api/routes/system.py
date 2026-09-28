"""System health, security status, performance, backtests and the live event stream."""

from __future__ import annotations

import asyncio
import json
import time
from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import desc, select

from app import __version__
from app.api.deps import AppContext, Auth, client_ip, ctx, current_auth, require_reauth
from app.api.routes.trading import engine_alive, wait_command
from app.api.serialize import to_dict
from app.backtest.data import load_funding, load_klines
from app.backtest.engine import CONTEXT, run_backtest, warmup_ms
from app.core import params as P
from app.db import models as M
from app.db.session import ping
from app.db.types import utcnow
from app.exchange.credentials import credential_status
from app.performance.metrics import TradeRow, compute

router = APIRouter(prefix="/api", tags=["system"])
_pool: ProcessPoolExecutor | None = None
_bt_lock = asyncio.Lock()


@router.get("/health")
async def health(c: AppContext = Depends(ctx)) -> dict:
    """Unauthenticated liveness probe — deliberately minimal."""
    db = await ping()
    return {"status": "ok" if db else "degraded", "db": db}


@router.get("/system/health")
async def system_health(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    comps = await c.store.components()
    now = utcnow()
    engine_ok = await engine_alive(c)
    st = await c.store.trading_state()
    live = await c.store.live_state(["engine", "ticker", "evaluation", "reconcile_paper", "reconcile_live"])

    def comp(name: str, stale_s: int = 90) -> dict[str, Any]:
        r = comps.get(name)
        if r is None:
            return {"status": "unknown", "detail": "no report yet", "updated_at": None}
        status = r.status if (now - r.updated_at).total_seconds() < stale_s else "stale"
        return {"status": status, "detail": r.detail, "updated_at": r.updated_at.isoformat(),
                "last_ok_at": r.last_ok_at.isoformat() if r.last_ok_at else None, "data": r.data}

    db_ok = await ping()
    async with c.store.factory() as s:
        last_trade = (await s.execute(select(M.Trade).where(M.Trade.opened_at.is_not(None))
                                      .order_by(desc(M.Trade.opened_at)).limit(1))).scalar_one_or_none()
        last_signal = (await s.execute(select(M.Signal).order_by(desc(M.Signal.id)).limit(1))).scalar_one_or_none()
    ticker = live.get("ticker", {})
    return {
        "version": __version__,
        "components": {
            "database": {"status": "ok" if db_ok else "down", "detail": "reachable" if db_ok else "unreachable"},
            "engine": comp("engine", 30) if engine_ok else {**comp("engine", 30), "status": "down"},
            "binance": comp("binance", 900),
            "market_ws": comp("market_ws", 30),
            "openai": comp("openai", 120),
            "notifications": comp("notifications", 60),
            "strategy": comp("strategy", 700),
            "trading": comp("trading", 30),
            "backup": comp("backup", 36 * 3600),
        },
        "trading": {"mode": st["mode"], "kill_switch": st["kill_switch"], "halted": st["halted"],
                    "halt_reason": st["halt_reason"], "strategy_enabled": st["strategy_enabled"]},
        "engine": live.get("engine"),
        "last_market_update": ticker.get("_updated_at"),
        "last_evaluation": live.get("evaluation", {}).get("evaluated_at"),
        "last_trade": to_dict(last_trade, {"snapshot", "protection"}) if last_trade else None,
        "last_signal": to_dict(last_signal, {"context", "risk_result"}) if last_signal else None,
        "reconcile": {"paper": live.get("reconcile_paper"), "live": live.get("reconcile_live")},
    }


@router.get("/system/events")
async def system_events(limit: int = 100, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> list[dict]:
    async with c.store.factory() as s:
        rows = (await s.execute(select(M.SystemEvent).order_by(desc(M.SystemEvent.id)).limit(min(limit, 500)))).scalars().all()
    return [to_dict(r) for r in rows]


@router.post("/system/events/ack")
async def ack_events(request: Request, auth: Auth = Depends(require_reauth), c: AppContext = Depends(ctx)) -> dict:
    """Mark critical events as reviewed (does not clear a halt)."""
    await c.store.resolve_critical()
    await c.store.audit("critical_events_acknowledged", user=auth.user, ip=client_ip(request))
    return {"ok": True}


# ----------------------------------------------------------------------------- security
@router.get("/security/status")
async def security_status(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    cred = await credential_status(c.store, c.settings)
    st = await c.store.trading_state()
    async with c.store.factory() as s:
        audit = (await s.execute(select(M.AuditLog).order_by(desc(M.AuditLog.id)).limit(50))).scalars().all()
    connection = cred["status"]
    return {
        "binance": {**cred, "configured": c.settings.binance_configured, "testnet": c.settings.binance_testnet,
                    "display_status": connection if connection != "connected" else ("live trading" if st["mode"] == "live"
                                                                                    else "connected (paper trading)")},
        "openai_configured": c.settings.openai_configured,
        "vapid_configured": c.settings.vapid_configured,
        "mode": st["mode"],
        "totp_enabled": auth.user.totp_enabled,
        "cookie_secure": c.settings.cookie_secure,
        "allowed_origins": c.settings.origins,
        "audit": [to_dict(a) for a in audit],
        "ip_guidance": "Restrict the Binance API key to your homelab's public IPv4 address "
                       "(Binance → API Management → Edit restrictions → Restrict access to trusted IPs only).",
    }


@router.post("/security/binance/verify")
async def verify_binance(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    if not c.limiter.hit(f"verify:{auth.user.id}", 6, 300):
        raise HTTPException(429, "slow down")
    cid = await c.store.enqueue_command("verify_credentials", {}, auth.user.username)
    await wait_command(c, cid, 30)
    return await credential_status(c.store, c.settings)


@router.post("/security/protection-test")
async def protection_test(request: Request, auth: Auth = Depends(require_reauth), c: AppContext = Depends(ctx)) -> dict:
    cid = await c.store.enqueue_command("protection_test", {}, auth.user.username)
    await c.store.audit("protection_test", user=auth.user, ip=client_ip(request))
    return await wait_command(c, cid, 45) or {"status": "pending"}


@router.post("/security/reconcile")
async def reconcile(request: Request, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    cid = await c.store.enqueue_command("reconcile", {"force": True}, auth.user.username)
    await c.store.audit("reconcile_requested", user=auth.user, ip=client_ip(request))
    res = await wait_command(c, cid, 60)
    live = await c.store.live_state(["reconcile_paper", "reconcile_live"])
    return {"result": res, **live}


# ----------------------------------------------------------------------------- performance
@router.get("/performance")
async def performance(mode: Literal["paper", "live"] = "paper", period: Literal["today", "7d", "30d", "all"] = "all",
                      auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    now = utcnow()
    since = {"today": now.replace(hour=0, minute=0, second=0, microsecond=0), "7d": now - timedelta(days=7),
             "30d": now - timedelta(days=30), "all": datetime(2000, 1, 1, tzinfo=UTC)}[period]
    async with c.store.factory() as s:
        trades = (await s.execute(select(M.Trade).where(M.Trade.mode == mode, M.Trade.status == "closed",
                                                        M.Trade.closed_at >= since).order_by(M.Trade.closed_at))).scalars().all()
        eq = (await s.execute(select(M.EquitySnapshot).where(M.EquitySnapshot.mode == mode, M.EquitySnapshot.ts >= since)
                              .order_by(M.EquitySnapshot.ts))).scalars().all()
    rows = [TradeRow(t.direction, t.opened_at or t.created_at, t.closed_at, t.realized_pnl, t.fees, t.funding,
                     t.r_multiple, t.entry_price, t.exit_price, t.exit_reason, t.id) for t in trades]
    if eq:
        start_eq = eq[0].equity
    elif trades and trades[0].equity_at_entry:
        start_eq = trades[0].equity_at_entry
    else:
        start_eq = c.settings.paper_starting_balance
    m = compute(rows, start_eq, [(e.ts, e.equity) for e in eq])
    if eq:  # use the sampled equity curve (includes unrealised swings) for charts
        m["equity_curve"] = [(int(e.ts.timestamp()), round(e.equity, 2)) for e in eq[:: max(1, len(eq) // 1500)]]
        pk = eq[0].equity
        dd = []
        for e in eq[:: max(1, len(eq) // 1500)]:
            pk = max(pk, e.equity)
            dd.append((int(e.ts.timestamp()), round((e.equity - pk) / pk * 100, 3) if pk else 0))
        m["drawdown_curve"] = dd
    m["mode"], m["period"], m["starting_equity"] = mode, period, round(start_eq, 2)
    m["disclaimer"] = "Past performance does not guarantee future results."
    return m


# ----------------------------------------------------------------------------- backtests
class BacktestIn(BaseModel):
    start: str = Field(description="YYYY-MM-DD")
    end: str = Field(description="YYYY-MM-DD")
    timeframe: Literal["5m", "15m"] = "5m"
    starting_balance: float = Field(default=10_000, ge=100, le=10_000_000)
    risk_per_trade_pct: float | None = Field(default=None, ge=0.05, le=2.0)
    strategy_version: int | None = None


@router.post("/backtest")
async def start_backtest(body: BacktestIn, request: Request, auth: Auth = Depends(current_auth),
                         c: AppContext = Depends(ctx)) -> dict:
    try:
        start = datetime.fromisoformat(body.start).replace(tzinfo=UTC)
        end = datetime.fromisoformat(body.end).replace(tzinfo=UTC) + timedelta(days=1)
    except ValueError as e:
        raise HTTPException(400, "dates must be YYYY-MM-DD") from e
    end = min(end, utcnow())
    if end <= start or (end - start).days > 365:
        raise HTTPException(400, "range must be 1–365 days")
    if start < datetime(2020, 1, 1, tzinfo=UTC):
        raise HTTPException(400, "start must be 2020 or later")
    if _bt_lock.locked():
        raise HTTPException(409, "a backtest is already running")
    async with c.store.factory() as s:
        if body.strategy_version:
            cfg = (await s.execute(select(M.StrategyConfig).where(M.StrategyConfig.version == body.strategy_version))
                   ).scalar_one_or_none()
            if cfg is None:
                raise HTTPException(404, "strategy version not found")
        else:
            cfg = None
    if cfg is None:
        cfg, _ = await c.store.active_params()
    params = P.enforce(cfg.params)
    if body.risk_per_trade_pct:
        params["risk_per_trade_pct"] = body.risk_per_trade_pct
    run = M.BacktestRun(status="queued", params={**body.model_dump(), "strategy_version": cfg.version,
                                                 "params": params})
    async with c.store.factory() as s:
        s.add(run)
        await s.commit()
        await s.refresh(run)
    await c.store.audit("backtest_started", user=auth.user, ip=client_ip(request), target=str(run.id))
    asyncio.create_task(_run_job(c, run.id, params, int(start.timestamp() * 1000), int(end.timestamp() * 1000),
                                 body.starting_balance, body.timeframe))
    return {"id": run.id, "status": "queued"}


async def _run_job(c: AppContext, rid: int, params: dict, start_ms: int, end_ms: int, balance: float, tf: str) -> None:
    global _pool
    async with _bt_lock:
        async def upd(**kw: Any) -> None:
            async with c.store.factory() as s:
                r = await s.get(M.BacktestRun, rid)
                for k, v in kw.items():
                    setattr(r, k, v)
                await s.commit()

        await upd(status="running")
        try:
            data = {}
            for ctf in CONTEXT[tf]:
                data[ctf] = await load_klines(c.public, c.store.factory, c.settings.symbol, ctf,
                                              start_ms - warmup_ms(ctf), end_ms)
            funding = await load_funding(c.public, c.store.factory, c.settings.symbol, start_ms, end_ms)
            await upd(progress=0.3)
            if _pool is None:
                _pool = ProcessPoolExecutor(max_workers=1)
            loop = asyncio.get_running_loop()
            t0 = time.time()
            result = await loop.run_in_executor(_pool, run_backtest, data, funding, params, start_ms, end_ms,
                                                balance, tf)
            result["wall_s"] = round(time.time() - t0, 1)
            json.dumps(result)  # fail early if not serialisable
            await upd(status="done", result=result, progress=1.0, finished_at=utcnow())
        except Exception as e:  # noqa: BLE001
            await upd(status="failed", error=f"{type(e).__name__}: {e}"[:2000], finished_at=utcnow())


@router.get("/backtest")
async def list_backtests(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> list[dict]:
    async with c.store.factory() as s:
        rows = (await s.execute(select(M.BacktestRun).order_by(desc(M.BacktestRun.id)).limit(30))).scalars().all()
    out = []
    for r in rows:
        d = to_dict(r, {"result"})
        d["params"] = {k: v for k, v in (r.params or {}).items() if k != "params"}
        if r.result:
            d["summary"] = {k: r.result["metrics"].get(k) for k in ("trades", "pnl", "roi_pct", "win_rate",
                                                                   "profit_factor", "max_drawdown_pct")}
        out.append(d)
    return out


@router.get("/backtest/{rid}")
async def get_backtest(rid: int, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        r = await s.get(M.BacktestRun, rid)
    if r is None:
        raise HTTPException(404, "not found")
    return to_dict(r)


# ----------------------------------------------------------------------------- live stream
@router.get("/stream")
async def stream(request: Request, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> StreamingResponse:
    """Server-sent events: ticker every second, other state whenever it changes."""

    async def gen():  # noqa: ANN202
        last: dict[str, str] = {}
        started = time.time()
        yield "retry: 3000\n\n"
        while not await request.is_disconnected() and time.time() - started < 3600:
            try:
                live = await c.store.live_state(["ticker", "evaluation", "engine", "accounts"])
                st = await c.store.trading_state()
                live["trading"] = {**st, "_updated_at": json.dumps(st, sort_keys=True, default=str)}
                for key, val in live.items():
                    stamp = val.get("_updated_at", "")
                    if last.get(key) != stamp:
                        last[key] = stamp
                        yield f"event: {key}\ndata: {json.dumps(val, default=str)}\n\n"
            except Exception:  # noqa: BLE001 - DB blip: keep the stream alive
                yield ": db-unavailable\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})
