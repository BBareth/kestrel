from __future__ import annotations

import io
from datetime import timedelta

import segno
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select, update

from app.api import security as sec
from app.api.deps import CSRF_COOKIE, SESSION_COOKIE, AppContext, Auth, client_ip, ctx, current_auth, require_reauth
from app.db import models as M
from app.db.types import utcnow

router = APIRouter(prefix="/api/auth", tags=["auth"])

MAX_FAILED = 8
LOCK_MINUTES = 15


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)
    totp: str | None = Field(default=None, max_length=32)


class ReauthIn(BaseModel):
    password: str = Field(min_length=1, max_length=256)
    totp: str | None = Field(default=None, max_length=32)


class PasswordIn(BaseModel):
    current: str = Field(max_length=256)
    new: str = Field(min_length=1, max_length=256)


class CodeIn(BaseModel):
    code: str = Field(max_length=32)


def _set_cookies(resp: Response, c: AppContext, token: str, csrf: str) -> None:
    max_age = c.settings.session_ttl_hours * 3600
    resp.set_cookie(SESSION_COOKIE, token, max_age=max_age, httponly=True, secure=c.settings.cookie_secure,
                    samesite="strict", path="/")
    resp.set_cookie(CSRF_COOKIE, csrf, max_age=max_age, httponly=False, secure=c.settings.cookie_secure,
                    samesite="strict", path="/")


def _user_view(u: M.User, s: M.Session) -> dict:
    return {"username": u.username, "totp_enabled": u.totp_enabled, "csrf_token": s.csrf_token,
            "reauth_valid_until": (s.reauth_at + timedelta(seconds=300)).isoformat() if s.reauth_at else None,
            "created_at": u.created_at.isoformat(), "last_login_at": u.last_login_at.isoformat() if u.last_login_at else None}


def _check_second_factor(c: AppContext, user: M.User, code: str | None) -> tuple[bool, list | None]:
    """Returns (ok, updated_recovery_codes_or_None)."""
    if not user.totp_enabled:
        return True, None
    if not code:
        return False, None
    secret = sec.decrypt_secret(c.settings.auth_secret.get_secret_value(), user.totp_secret_enc or "")
    if secret and sec.verify_totp(secret, code):
        return True, None
    h = sec.token_hash(code.strip().lower())
    codes = list(user.recovery_codes or [])
    if h in codes:
        codes.remove(h)
        return True, codes
    return False, None


@router.post("/login")
async def login(body: LoginIn, request: Request, response: Response, c: AppContext = Depends(ctx)) -> dict:
    ip = client_ip(request)
    if not c.limiter.hit(f"login-ip:{ip}", 10, 300):
        raise HTTPException(429, "too many login attempts — wait a few minutes")
    now = utcnow()
    async with c.store.factory() as s:
        user = (await s.execute(select(M.User).where(M.User.username == body.username.strip()))).scalar_one_or_none()
        if user and user.locked_until and user.locked_until > now:
            raise HTTPException(423, "account temporarily locked after repeated failures")
        ok = sec.verify_password(user.password_hash if user else None, body.password)
        if not ok or user is None:
            if user:
                user.failed_logins += 1
                if user.failed_logins >= MAX_FAILED:
                    user.locked_until = now + timedelta(minutes=LOCK_MINUTES)
                    user.failed_logins = 0
                await s.commit()
            await c.store.audit("login_failed", username=body.username[:64], ip=ip)
            raise HTTPException(401, "invalid username or password")
        f2, new_codes = _check_second_factor(c, user, body.totp)
        if not f2:
            if body.totp:
                user.failed_logins += 1
                await s.commit()
                await c.store.audit("login_totp_failed", user=user, ip=ip)
                raise HTTPException(401, "invalid authentication code")
            return {"totp_required": True}
        if new_codes is not None:
            user.recovery_codes = new_codes
        user.failed_logins = 0
        user.locked_until = None
        user.last_login_at = now
        if sec.needs_rehash(user.password_hash):
            user.password_hash = sec.hash_password(body.password)
        token, csrf = sec.new_token(), sec.new_token()
        sess = M.Session(user_id=user.id, token_hash=sec.token_hash(token), csrf_token=csrf, created_at=now,
                         last_seen_at=now, expires_at=now + timedelta(hours=c.settings.session_ttl_hours),
                         ip=ip, user_agent=(request.headers.get("user-agent") or "")[:300], reauth_at=now)
        s.add(sess)
        await s.commit()
        await s.refresh(sess)
    c.limiter.reset(f"login-ip:{ip}")
    await c.store.audit("login", user=user, ip=ip)
    _set_cookies(response, c, token, csrf)
    return {"ok": True, "user": _user_view(user, sess)}


@router.post("/logout")
async def logout(response: Response, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        await s.execute(update(M.Session).where(M.Session.id == auth.session.id).values(revoked=True))
        await s.commit()
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    await c.store.audit("logout", user=auth.user)
    return {"ok": True}


@router.get("/me")
async def me(auth: Auth = Depends(current_auth)) -> dict:
    return _user_view(auth.user, auth.session)


@router.post("/reauth")
async def reauth(body: ReauthIn, request: Request, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    if not c.limiter.hit(f"reauth:{auth.user.id}", 8, 300):
        raise HTTPException(429, "too many attempts")
    async with c.store.factory() as s:
        user = await s.get(M.User, auth.user.id)
        if not sec.verify_password(user.password_hash, body.password):
            await c.store.audit("reauth_failed", user=user, ip=client_ip(request))
            raise HTTPException(401, "wrong password")
        ok, codes = _check_second_factor(c, user, body.totp)
        if not ok:
            raise HTTPException(401, "authentication code required" if not body.totp else "invalid authentication code")
        if codes is not None:
            user.recovery_codes = codes
        sess = await s.get(M.Session, auth.session.id)
        sess.reauth_at = utcnow()
        await s.commit()
    await c.store.audit("reauth", user=auth.user, ip=client_ip(request))
    return {"ok": True, "valid_seconds": c.settings.reauth_window_seconds}


@router.post("/password")
async def change_password(body: PasswordIn, request: Request, auth: Auth = Depends(require_reauth),
                          c: AppContext = Depends(ctx)) -> dict:
    problems = sec.password_problems(body.new, auth.user.username)
    if problems:
        raise HTTPException(400, "password " + ", ".join(problems))
    async with c.store.factory() as s:
        user = await s.get(M.User, auth.user.id)
        if not sec.verify_password(user.password_hash, body.current):
            raise HTTPException(401, "current password is wrong")
        user.password_hash = sec.hash_password(body.new)
        user.password_changed_at = utcnow()
        # revoke every other session
        await s.execute(update(M.Session).where(M.Session.user_id == user.id, M.Session.id != auth.session.id)
                        .values(revoked=True))
        await s.commit()
    await c.store.audit("password_changed", user=auth.user, ip=client_ip(request))
    return {"ok": True}


@router.post("/totp/setup")
async def totp_setup(auth: Auth = Depends(require_reauth), c: AppContext = Depends(ctx)) -> dict:
    secret = sec.new_totp_secret()
    async with c.store.factory() as s:
        user = await s.get(M.User, auth.user.id)
        user.totp_pending_enc = sec.encrypt_secret(c.settings.auth_secret.get_secret_value(), secret)
        await s.commit()
    uri = sec.totp_uri(secret, auth.user.username)
    buf = io.BytesIO()
    segno.make(uri, error="m").save(buf, kind="svg", scale=5, border=2, dark="#0b0e12", light="#ffffff",
                                    xmldecl=False, svgns=True)
    return {"secret": secret, "uri": uri, "qr_svg": buf.getvalue().decode()}


@router.post("/totp/enable")
async def totp_enable(body: CodeIn, auth: Auth = Depends(require_reauth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        user = await s.get(M.User, auth.user.id)
        secret = sec.decrypt_secret(c.settings.auth_secret.get_secret_value(), user.totp_pending_enc or "")
        if not secret or not sec.verify_totp(secret, body.code):
            raise HTTPException(400, "code does not match — check the time on your phone and try again")
        codes = sec.new_recovery_codes()
        user.totp_secret_enc = user.totp_pending_enc
        user.totp_pending_enc = None
        user.totp_enabled = True
        user.recovery_codes = [sec.token_hash(x) for x in codes]
        await s.commit()
    await c.store.audit("totp_enabled", user=auth.user)
    return {"ok": True, "recovery_codes": codes}


@router.post("/totp/disable")
async def totp_disable(auth: Auth = Depends(require_reauth), c: AppContext = Depends(ctx)) -> dict:
    st = await c.store.trading_state()
    if st.get("mode") == "live":
        raise HTTPException(409, "switch to paper trading before disabling 2FA")
    async with c.store.factory() as s:
        user = await s.get(M.User, auth.user.id)
        user.totp_enabled = False
        user.totp_secret_enc = None
        user.recovery_codes = []
        await s.commit()
    await c.store.audit("totp_disabled", user=auth.user)
    return {"ok": True}


@router.get("/sessions")
async def sessions(auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> list[dict]:
    async with c.store.factory() as s:
        rows = (await s.execute(select(M.Session).where(M.Session.user_id == auth.user.id, M.Session.revoked.is_(False),
                                                        M.Session.expires_at > utcnow())
                                .order_by(M.Session.last_seen_at.desc()))).scalars().all()
    return [{"id": r.id, "created_at": r.created_at.isoformat(), "last_seen_at": r.last_seen_at.isoformat(),
             "ip": r.ip, "user_agent": r.user_agent, "current": r.id == auth.session.id} for r in rows]


@router.delete("/sessions/{sid}")
async def revoke_session(sid: int, auth: Auth = Depends(current_auth), c: AppContext = Depends(ctx)) -> dict:
    async with c.store.factory() as s:
        await s.execute(update(M.Session).where(M.Session.id == sid, M.Session.user_id == auth.user.id)
                        .values(revoked=True))
        await s.commit()
    await c.store.audit("session_revoked", user=auth.user, target=str(sid))
    return {"ok": True}
