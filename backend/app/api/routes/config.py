"""Strategy / risk / AI configuration, safety settings, notifications and AI usage."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, desc, func, select

from app.api.deps import AppContext, Auth, client_ip, ctx, current_auth
from app.api.serialize import to_dict
from app.core import params as P
from app.core.store import NOTIFY_DEFAULT, day_start
from app.db import models as M
from app.db.types import utcnow
from app.notifications.service import CATEGORIES

router = APIRouter(prefix="/api", tags=["config"])


class ParamsIn(BaseModel):
    params: dict[str, Any]
    note: str | None = Field(default=None, max_length=300)


def _reauth_ok(auth: Auth, c: AppContext) -> bool:
    ra = auth.session.reauth_at
    return ra is not None and (utcnow() - ra).total_seconds() <= c.settings.reauth_window_seconds


# ----------------------------------------------------------------------------- strategy
@router.get("/strategy/config")
async def get_config(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    cfg, params = await c.store.active_params()
    ready = await c.store.get_setting("readiness", {})
    return {"version": cfg.version, "id": cfg.id, "params": params, "defaults": P.defaults(),
            "catalogue": P.catalogue(), "created_at": cfg.created_at.isoformat(), "created_by": cfg.created_by,
            "note": cfg.note, "risk_reviewed": ready.get("risk_review")}


@router.put("/strategy/config")
async def put_config(body: ParamsIn, request: Request, auth: Auth = Depends(current_auth),
                     c: AppContext = Depends(ctx)) -> dict:
    cfg, current = await c.store.active_params()
    merged = {**current, **body.params}
    clean, errors = P.validate(merged)
    if errors:
        raise HTTPException(422, detail={"message": "invalid parameters", "errors": errors})
    changed = P.diff(current, clean).changed
    if not changed:
        return {"version": cfg.version, "changed": {}}
    st = await c.store.trading_state()
    sensitive = set(changed) & P.risk_sensitive_keys()
    if st["mode"] == "live" and sensitive and not _reauth_ok(auth, c):
        raise HTTPException(403, detail={"reauth_required": True,
                                         "message": "risk settings in LIVE mode need re-authentication"})
    row = await c.store.save_config(clean, auth.user.username, body.note)
    await c.store.audit("strategy_config_changed", user=auth.user, ip=client_ip(request), target=f"v{row.version}",
                        data={"changed": {k: [a, b] for k, (a, b) in changed.items()}})
    if sensitive:
        await c.store.update_setting("readiness", {"risk_review": None})
    return {"version": row.version, "changed": {k: [a, b] for k, (a, b) in changed.items()}}


@router.post("/strategy/reset")
async def reset_config(request: Request, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    row = await c.store.save_config(P.defaults(), auth.user.username, "reset to safe defaults")
    await c.store.audit("strategy_config_reset", user=auth.user, ip=client_ip(request), target=f"v{row.version}")
    return {"version": row.version, "params": row.params}


@router.post("/strategy/risk-reviewed")
async def risk_reviewed(request: Request, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    await c.store.mark_readiness("risk_review", True, f"by {auth.user.username}")
    await c.store.audit("risk_limits_reviewed", user=auth.user, ip=client_ip(request))
    return {"ok": True}


@router.get("/strategy/versions")
async def versions(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> list[dict]:
    async with c.store.factory() as s:
        rows = (await s.execute(select(M.StrategyConfig).order_by(desc(M.StrategyConfig.version)).limit(50))).scalars().all()
    return [to_dict(r) for r in rows]


@router.post("/strategy/versions/{version}/activate")
async def activate_version(version: int, request: Request, auth: Auth = Depends(current_auth),
                           c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        row = (await s.execute(select(M.StrategyConfig).where(M.StrategyConfig.version == version))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "no such version")
    st = await c.store.trading_state()
    if st["mode"] == "live" and not _reauth_ok(auth, c):
        raise HTTPException(403, detail={"reauth_required": True, "message": "re-authenticate to change live config"})
    new = await c.store.save_config(row.params, auth.user.username, f"restored from v{version}")
    await c.store.audit("strategy_config_restored", user=auth.user, ip=client_ip(request), target=f"v{version}")
    return {"version": new.version}


# ----------------------------------------------------------------------------- safety settings
@router.get("/settings")
async def get_settings_(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    return {"safety": await c.store.safety(), "catalogue": P.catalogue()["safety"],
            "notifications": await c.store.notify_prefs(), "categories": CATEGORIES,
            "symbol": c.settings.symbol, "paper_starting_balance": c.settings.paper_starting_balance,
            "testnet": c.settings.binance_testnet}


@router.put("/settings/safety")
async def put_safety(body: dict[str, Any], request: Request, auth: Auth = Depends(current_auth),
                     c: AppContext = Depends(ctx)) -> dict:
    current = await c.store.safety()
    clean, errors = P.validate_safety({**current, **body})
    if errors:
        raise HTTPException(422, detail={"message": "invalid settings", "errors": errors})
    st = await c.store.trading_state()
    if st["mode"] == "live" and not _reauth_ok(auth, c):
        raise HTTPException(403, detail={"reauth_required": True, "message": "re-authenticate to change live safety"})
    await c.store.set_setting("safety", clean, auth.user.username)
    await c.store.audit("safety_settings_changed", user=auth.user, ip=client_ip(request),
                        data={k: v for k, v in clean.items() if current.get(k) != v})
    return {"safety": clean}


# ----------------------------------------------------------------------------- notifications
class SubIn(BaseModel):
    endpoint: str = Field(max_length=1000)
    keys: dict[str, str]


@router.get("/notifications")
async def notifications(limit: int = Query(100, le=500), auth: Auth = Depends(current_auth),
                        c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        rows = (await s.execute(select(M.Notification).order_by(desc(M.Notification.id)).limit(limit))).scalars().all()
        subs = (await s.execute(select(M.PushSubscription).where(M.PushSubscription.user_id == auth.user.id))).scalars().all()
    return {"items": [to_dict(r) for r in rows], "vapid_public_key": c.settings.vapid_public_key or None,
            "configured": c.settings.vapid_configured, "prefs": await c.store.notify_prefs(), "categories": CATEGORIES,
            "devices": [{"id": x.id, "user_agent": x.user_agent, "created_at": x.created_at.isoformat(),
                         "endpoint_host": x.endpoint.split("/")[2] if "//" in x.endpoint else "?",
                         "last_success_at": x.last_success_at.isoformat() if x.last_success_at else None,
                         "failure_count": x.failure_count} for x in subs]}


@router.put("/notifications/prefs")
async def put_prefs(body: dict[str, bool], request: Request, auth: Auth = Depends(current_auth),
                    c: AppContext = Depends(ctx)) -> dict:
    clean = {k: bool(v) for k, v in body.items() if k in NOTIFY_DEFAULT}
    prefs = await c.store.update_setting("notifications", clean, NOTIFY_DEFAULT, auth.user.username)
    await c.store.audit("notification_prefs_changed", user=auth.user, ip=client_ip(request), data=clean)
    return prefs


@router.post("/notifications/subscribe")
async def subscribe(body: SubIn, request: Request, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    if not body.endpoint.startswith("https://") or "p256dh" not in body.keys or "auth" not in body.keys:
        raise HTTPException(400, "invalid push subscription")
    async with c.store.factory() as s:
        existing = (await s.execute(select(M.PushSubscription).where(M.PushSubscription.endpoint == body.endpoint))
                    ).scalar_one_or_none()
        if existing:
            existing.p256dh, existing.auth, existing.user_id = body.keys["p256dh"][:200], body.keys["auth"][:100], auth.user.id
        else:
            s.add(M.PushSubscription(user_id=auth.user.id, endpoint=body.endpoint, p256dh=body.keys["p256dh"][:200],
                                     auth=body.keys["auth"][:100],
                                     user_agent=(request.headers.get("user-agent") or "")[:300]))
        await s.commit()
    await c.store.audit("push_subscribed", user=auth.user, ip=client_ip(request))
    return {"ok": True}


@router.post("/notifications/unsubscribe")
async def unsubscribe(body: dict[str, str], auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        await s.execute(delete(M.PushSubscription).where(M.PushSubscription.endpoint == body.get("endpoint", ""),
                                                         M.PushSubscription.user_id == auth.user.id))
        await s.commit()
    return {"ok": True}


@router.delete("/notifications/devices/{did}")
async def remove_device(did: int, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        await s.execute(delete(M.PushSubscription).where(M.PushSubscription.id == did,
                                                         M.PushSubscription.user_id == auth.user.id))
        await s.commit()
    return {"ok": True}


@router.post("/notifications/test")
async def test_notification(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    if not c.limiter.hit(f"pushtest:{auth.user.id}", 5, 60):
        raise HTTPException(429, "slow down")
    n = await c.notifier.send_test()
    async with c.store.factory() as s:
        row = await s.get(M.Notification, n.id)
    if row.delivered > 0:
        await c.store.mark_readiness("notification_test", True, f"delivered to {row.delivered} device(s)")
    return {"delivered": row.delivered, "failed": row.failed, "error": row.error}


# ----------------------------------------------------------------------------- AI
@router.get("/ai/analyses")
async def analyses(limit: int = Query(50, le=200), auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> list[dict]:
    async with c.store.factory() as s:
        rows = (await s.execute(select(M.AIAnalysis).order_by(desc(M.AIAnalysis.id)).limit(limit))).scalars().all()
    return [to_dict(r, {"request"}) for r in rows]


@router.get("/ai/analyses/{aid}")
async def analysis(aid: int, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        r = await s.get(M.AIAnalysis, aid)
    if r is None:
        raise HTTPException(404, "not found")
    return to_dict(r)


@router.get("/ai/usage")
async def ai_usage(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    _, p = await c.store.active_params()
    now = utcnow()
    today, n_today = await c.store.ai_spend_since(day_start(now))
    week, n_week = await c.store.ai_spend_since(now - timedelta(days=7))
    month, n_month = await c.store.ai_spend_since(now - timedelta(days=30))
    async with c.store.factory() as s:
        by_trigger = (await s.execute(select(M.AIAnalysis.trigger, func.count(), func.sum(M.AIAnalysis.cost_usd))
                                      .where(M.AIAnalysis.ts >= now - timedelta(days=30))
                                      .group_by(M.AIAnalysis.trigger))).all()
        daily = (await s.execute(select(M.AIAnalysis.ts, M.AIAnalysis.cost_usd)
                                 .where(M.AIAnalysis.ts >= now - timedelta(days=30)))).all()
        invalid = (await s.execute(select(func.count()).select_from(M.AIAnalysis).where(
            M.AIAnalysis.valid.is_(False), M.AIAnalysis.ts >= now - timedelta(days=30)))).scalar() or 0
    per_day: dict[str, float] = {}
    for ts, cost in daily:
        k = ts.date().isoformat()
        per_day[k] = per_day.get(k, 0.0) + (cost or 0.0)
    comps = await c.store.components()
    oa = comps.get("openai")
    return {
        "configured": c.settings.openai_configured, "mode": p["ai_mode"], "model": p["ai_model"],
        "budget_usd": p["ai_daily_budget_usd"], "today_usd": round(today, 4), "today_calls": n_today,
        "week_usd": round(week, 4), "week_calls": n_week, "month_usd": round(month, 4), "month_calls": n_month,
        "budget_exceeded": today >= p["ai_daily_budget_usd"], "invalid_30d": invalid,
        "by_trigger": [{"trigger": t, "calls": n, "cost_usd": round(cst or 0, 4)} for t, n, cst in by_trigger],
        "per_day": [{"day": k, "cost_usd": round(v, 4)} for k, v in sorted(per_day.items())],
        "status": oa.status if oa else "unknown", "detail": oa.detail if oa else None,
    }


@router.post("/ai/analyze")
async def analyze_now(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    if not c.limiter.hit(f"ai-manual:{auth.user.id}", 3, 300):
        raise HTTPException(429, "manual analyses are limited to 3 per 5 minutes")
    from app.api.routes.trading import wait_command

    cid = await c.store.enqueue_command("analyze_now", {"trigger": "manual"}, auth.user.username)
    return await wait_command(c, cid, 120) or {"status": "pending"}
