from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.deps import AppContext, Auth, client_ip, ctx, current_auth, require_reauth
from app.api.security import consteq, token_hash
from app.core.readiness import compute_readiness
from app.db.types import utcnow
from app.execution.engine import ExecutionEngine

router = APIRouter(prefix="/api/trading", tags=["trading"])

CONFIRM_TEXT = "ENABLE LIVE TRADING"


class KillIn(BaseModel):
    reason: str = Field(default="manual kill switch", max_length=200)


class ToggleIn(BaseModel):
    enabled: bool


class LiveEnableIn(BaseModel):
    token: str = Field(max_length=100)
    confirmation: str = Field(max_length=64)
    acknowledge_risk: bool


class LiveDisableIn(BaseModel):
    close_positions: bool = False


class ResetIn(BaseModel):
    balance: float = Field(default=10_000, ge=100, le=10_000_000)


async def wait_command(c: AppContext, cid: int, timeout: float = 20) -> dict | None:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        cmd = await c.store.get_command(cid)
        if cmd and cmd.status != "pending":
            return {"status": cmd.status, **(cmd.result or {})}
        await asyncio.sleep(0.25)
    return None


async def engine_alive(c: AppContext) -> bool:
    comps = await c.store.components()
    e = comps.get("engine")
    return e is not None and e.updated_at > utcnow() - timedelta(seconds=20)


@router.get("/state")
async def state(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    st = await c.store.trading_state()
    live = await c.store.live_state(["engine"])
    return {**st, "engine": live.get("engine", {}), "engine_alive": await engine_alive(c),
            "live_possible": c.settings.binance_configured, "testnet": c.settings.binance_testnet}


@router.post("/kill")
async def kill(body: KillIn, request: Request, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    """Immediate: no re-authentication, so it is always one tap away."""
    by = auth.user.username
    await c.store.update_trading({"kill_switch": True, "strategy_enabled": False,
                                  "kill_engaged_at": utcnow().isoformat()}, by)
    await c.store.audit("kill_switch", user=auth.user, ip=client_ip(request), data={"reason": body.reason})
    cid = await c.store.enqueue_command("kill", {"reason": body.reason}, by)
    result = await wait_command(c, cid, 20)
    if result is None and not await engine_alive(c):
        result = await _fallback_kill(c, body.reason)
    return {"ok": True, "engine": result, "message": "Kill switch engaged — strategy disabled, no new trades."}


async def _fallback_kill(c: AppContext, reason: str) -> dict:
    """Engine is down: act on the live venue directly from the API process."""
    st = await c.store.trading_state()
    sf = await c.store.safety()
    if c.live_adapter is None or not (st.get("mode") == "live" or await c.store.open_trades("live")):
        return {"status": "fallback", "actions": ["engine offline; paper state frozen until engine restarts"]}

    async def halt(r: str) -> None:
        await c.store.update_trading({"halted": True, "halt_reason": r})

    salt = (await c.store.get_setting("install", {})).get("salt", "x0")
    execu = ExecutionEngine(c.live_adapter, c.store, c.notifier, c.settings.symbol, halt, salt=salt)
    actions = []
    if sf["kill_positions"] == "close":
        ok = await execu.emergency_flatten(f"kill switch via API fallback ({reason})")
        actions.append(f"live flattened={ok}")
    if sf["kill_to_paper"]:
        await c.store.update_trading({"mode": "paper"})
        actions.append("mode → PAPER")
    await c.store.system_event("critical", "api", "kill_fallback", "kill switch executed by API (engine offline)",
                               {"actions": actions})
    await c.notifier.notify("risk", "KILL SWITCH engaged", "Engine offline — API executed: " + "; ".join(actions),
                            "critical")
    return {"status": "fallback", "actions": actions}


@router.post("/resume")
async def resume(request: Request, auth: Auth = Depends(require_reauth), c: AppContext = Depends(ctx)) -> dict:
    """Release the kill switch. The strategy stays disabled until explicitly enabled."""
    await c.store.update_trading({"kill_switch": False}, auth.user.username)
    await c.store.audit("kill_switch_released", user=auth.user, ip=client_ip(request))
    await c.store.system_event("warning", "safety", "kill_released", f"kill switch released by {auth.user.username}")
    return {"ok": True}


@router.post("/strategy")
async def toggle_strategy(body: ToggleIn, request: Request, auth: Auth = Depends(current_auth),
                          c: AppContext = Depends(ctx)) -> dict:
    st = await c.store.trading_state()
    if body.enabled:
        if st["kill_switch"]:
            raise HTTPException(409, "release the kill switch first")
        if st["halted"]:
            raise HTTPException(409, f"trading is halted: {st['halt_reason']} — clear the halt first")
        if st["mode"] == "live":
            ra = auth.session.reauth_at
            if ra is None or (utcnow() - ra).total_seconds() > c.settings.reauth_window_seconds:
                raise HTTPException(403, detail={"reauth_required": True, "message": "re-authenticate to arm live trading"})
    await c.store.update_trading({"strategy_enabled": body.enabled}, auth.user.username)
    await c.store.audit("strategy_enabled" if body.enabled else "strategy_disabled", user=auth.user,
                        ip=client_ip(request), data={"mode": st["mode"]})
    return {"ok": True, "strategy_enabled": body.enabled}


@router.post("/halt/clear")
async def clear_halt(request: Request, auth: Auth = Depends(require_reauth), c: AppContext = Depends(ctx)) -> dict:
    st = await c.store.trading_state()
    await c.store.update_trading({"halted": False, "halt_reason": None, "halted_at": None}, auth.user.username)
    await c.store.resolve_critical()
    await c.store.audit("halt_cleared", user=auth.user, ip=client_ip(request), data={"was": st.get("halt_reason")})
    await c.store.system_event("warning", "safety", "halt_cleared",
                               f"halt cleared by {auth.user.username} (was: {st.get('halt_reason')})")
    return {"ok": True}


@router.get("/readiness")
async def readiness(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    return await compute_readiness(c.store, c.settings, auth.user)


@router.post("/live/unlock")
async def live_unlock(request: Request, auth: Auth = Depends(require_reauth), c: AppContext = Depends(ctx)) -> dict:
    """Step 1: re-authenticated user + passing checklist → short-lived unlock token."""
    if not c.settings.binance_configured:
        raise HTTPException(409, "no Binance credentials configured")
    r = await compute_readiness(c.store, c.settings, auth.user)
    if not r["ready"]:
        failing = [i["label"] for i in r["items"] if i["required"] and not i["ok"]]
        raise HTTPException(409, detail={"message": "live readiness checklist not complete", "failing": failing})
    token = secrets.token_urlsafe(24)
    await c.store.set_setting("live_unlock", {"hash": token_hash(token), "session": auth.session.id,
                                              "expires": (utcnow() + timedelta(minutes=5)).isoformat()})
    await c.store.audit("live_unlock_requested", user=auth.user, ip=client_ip(request))
    return {"token": token, "confirmation_text": CONFIRM_TEXT, "expires_in": 300}


@router.post("/live/enable")
async def live_enable(body: LiveEnableIn, request: Request, auth: Auth = Depends(require_reauth),
                      c: AppContext = Depends(ctx)) -> dict:
    """Step 2: token + typed confirmation + risk acknowledgement → LIVE (strategy stays disarmed)."""
    rec = await c.store.get_setting("live_unlock", {})
    if not rec or rec.get("session") != auth.session.id or not consteq(rec.get("hash", ""), token_hash(body.token)) \
            or datetime.fromisoformat(rec["expires"]) < utcnow():
        raise HTTPException(403, "unlock token invalid or expired — start again")
    if body.confirmation != CONFIRM_TEXT:
        raise HTTPException(400, f"type exactly: {CONFIRM_TEXT}")
    if not body.acknowledge_risk:
        raise HTTPException(400, "you must acknowledge the risks")
    r = await compute_readiness(c.store, c.settings, auth.user)
    if not r["ready"]:
        raise HTTPException(409, "readiness checklist no longer passes")
    await c.store.set_setting("live_unlock", {})
    await c.store.update_trading({"mode": "live", "strategy_enabled": False, "kill_switch": False,
                                  "live_enabled_at": utcnow().isoformat(), "live_enabled_by": auth.user.username},
                                 auth.user.username)
    await c.store.audit("live_enabled", user=auth.user, ip=client_ip(request))
    await c.store.system_event("warning", "safety", "live_enabled", f"LIVE TRADING ENABLED by {auth.user.username}")
    await c.notifier.notify("live_mode", "Live trading enabled",
                            f"LIVE mode enabled by {auth.user.username}. Strategy is disarmed until you arm it.",
                            "critical", {"url": "/trading"})
    return {"ok": True, "mode": "live", "strategy_enabled": False}


@router.post("/live/disable")
async def live_disable(body: LiveDisableIn, request: Request, auth: Auth = Depends(current_auth),
                       c: AppContext = Depends(ctx)) -> dict:
    open_live = await c.store.open_trades("live")
    if open_live and not body.close_positions:
        raise HTTPException(409, "a live position is open — close it (or use the kill switch) before leaving live mode")
    for t in open_live:
        cid = await c.store.enqueue_command("close_trade", {"trade_id": t.id}, auth.user.username)
        await wait_command(c, cid, 30)
    await c.store.update_trading({"mode": "paper"}, auth.user.username)
    await c.store.audit("live_disabled", user=auth.user, ip=client_ip(request))
    await c.notifier.notify("live_mode", "Live trading disabled", "Back to PAPER trading.", "warning")
    return {"ok": True, "mode": "paper"}


@router.post("/close/{trade_id}")
async def close_trade(trade_id: int, request: Request, auth: Auth = Depends(current_auth),
                      c: AppContext = Depends(ctx)) -> dict:
    cid = await c.store.enqueue_command("close_trade", {"trade_id": trade_id}, auth.user.username)
    await c.store.audit("close_trade", user=auth.user, ip=client_ip(request), target=str(trade_id))
    res = await wait_command(c, cid, 30)
    if res is None:
        raise HTTPException(504, "engine did not respond — check System")
    return res


@router.post("/evaluate")
async def evaluate_now(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    if not c.limiter.hit(f"eval:{auth.user.id}", 6, 60):
        raise HTTPException(429, "slow down")
    cid = await c.store.enqueue_command("evaluate_now", {}, auth.user.username)
    return await wait_command(c, cid, 30) or {"status": "pending"}


@router.post("/paper/reset")
async def paper_reset(body: ResetIn, request: Request, auth: Auth = Depends(require_reauth),
                      c: AppContext = Depends(ctx)) -> dict:
    cid = await c.store.enqueue_command("paper_reset", {"balance": body.balance}, auth.user.username)
    await c.store.audit("paper_reset", user=auth.user, ip=client_ip(request), data={"balance": body.balance})
    return await wait_command(c, cid, 20) or {"status": "pending"}
