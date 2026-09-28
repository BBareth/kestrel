"""FastAPI dependencies: application context, authentication, CSRF, step-up re-auth."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from app.api.security import RateLimiter, consteq, token_hash
from app.config import Settings
from app.core.store import Store
from app.db import models as M
from app.db.types import utcnow

SESSION_COOKIE = "kestrel_session"
CSRF_COOKIE = "kestrel_csrf"
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


@dataclass
class AppContext:
    settings: Settings
    store: Store
    limiter: RateLimiter
    notifier: object
    live_adapter: object | None
    public: object


@dataclass
class Auth:
    user: M.User
    session: M.Session


def ctx(request: Request) -> AppContext:
    return request.app.state.ctx


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


async def current_auth(request: Request, c: AppContext = Depends(ctx)) -> Auth:
    token = request.cookies.get(SESSION_COOKIE)
    if not token or len(token) > 200:
        raise HTTPException(401, "not authenticated")
    now = utcnow()
    async with c.store.factory() as s:
        sess = (await s.execute(select(M.Session).where(M.Session.token_hash == token_hash(token)))).scalar_one_or_none()
        if sess is None or sess.revoked or sess.expires_at < now or \
                sess.last_seen_at < now - timedelta(hours=c.settings.session_idle_hours):
            raise HTTPException(401, "session expired")
        user = await s.get(M.User, sess.user_id)
        if user is None:
            raise HTTPException(401, "not authenticated")
        if (now - sess.last_seen_at).total_seconds() > 60:
            sess.last_seen_at = now
            await s.commit()
        s.expunge(user)
        s.expunge(sess)
    # CSRF: every state-changing request needs the per-session token in a header,
    # and (when the browser sends one) an allowed Origin.
    if request.method not in SAFE_METHODS:
        header = request.headers.get("x-csrf-token", "")
        if not header or not consteq(header, sess.csrf_token):
            raise HTTPException(403, "CSRF token missing or invalid")
        origin = request.headers.get("origin")
        if origin and c.settings.origins and origin.rstrip("/") not in c.settings.origins:
            raise HTTPException(403, "origin not allowed")
    if not c.limiter.hit(f"api:{sess.id}", 600, 60):
        raise HTTPException(429, "too many requests")
    return Auth(user, sess)


async def require_reauth(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> Auth:
    """Step-up authentication: password (+TOTP) re-entered within the last few minutes."""
    ra = auth.session.reauth_at
    if ra is None or (utcnow() - ra).total_seconds() > c.settings.reauth_window_seconds:
        raise HTTPException(403, detail={"reauth_required": True, "message": "re-enter your password to continue"})
    return auth
