from __future__ import annotations

import pyotp
import pytest
from httpx import ASGITransport, AsyncClient

from app.api.main import create_app
from app.api.security import hash_password
from app.config import Settings
from app.db import models as M
from app.db.types import utcnow

PW = "correct-horse-battery-9"


@pytest.fixture
async def client(store):
    settings = Settings(auth_secret="t" * 48, cookie_secure=False, allowed_origins="http://testserver",
                        environment="test", database_url="sqlite+aiosqlite://")
    app = create_app(settings, init_db=False)
    async with store.factory() as s:
        s.add(M.User(username="admin", password_hash=hash_password(PW)))
        await s.commit()
    async with app.router.lifespan_context(app):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            c.store = store  # type: ignore[attr-defined]
            yield c


async def login(c: AsyncClient, pw: str = PW, totp: str | None = None) -> dict:
    r = await c.post("/api/auth/login", json={"username": "admin", "password": pw, "totp": totp})
    return {"status": r.status_code, "json": r.json(), "headers": r.headers}


def csrf(c: AsyncClient) -> dict:
    return {"X-CSRF-Token": c.cookies.get("kestrel_csrf")}


async def test_health_is_public_and_minimal(client):
    r = await client.get("/api/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"
    assert set(r.json()) == {"status", "db"}


async def test_protected_routes_require_session(client):
    for path in ("/api/dashboard", "/api/trading/state", "/api/strategy/config", "/api/security/status",
                 "/api/system/health", "/api/trades", "/api/stream"):
        assert (await client.get(path)).status_code == 401, path
    assert (await client.post("/api/trading/kill", json={})).status_code == 401


async def test_login_sets_secure_cookie_flags(client):
    res = await login(client)
    assert res["status"] == 200 and res["json"]["ok"]
    sc = res["headers"].get_list("set-cookie")
    sess = next(x for x in sc if x.startswith("kestrel_session="))
    assert "HttpOnly" in sess and "samesite=strict" in sess.lower()
    csrf_cookie = next(x for x in sc if x.startswith("kestrel_csrf="))
    assert "HttpOnly" not in csrf_cookie
    assert (await client.get("/api/auth/me")).json()["username"] == "admin"


async def test_wrong_password_and_lockout(client):
    for _ in range(8):
        assert (await login(client, "wrong-password-xx"))["status"] in (401, 429)
    r = await login(client)
    assert r["status"] in (423, 429)


async def test_csrf_required_for_state_changes(client):
    await login(client)
    r = await client.post("/api/trading/strategy", json={"enabled": False})
    assert r.status_code == 403
    r = await client.post("/api/trading/strategy", json={"enabled": False}, headers={"X-CSRF-Token": "forged"})
    assert r.status_code == 403
    r = await client.post("/api/trading/strategy", json={"enabled": False}, headers=csrf(client))
    assert r.status_code == 200


async def test_foreign_origin_rejected(client):
    await login(client)
    r = await client.post("/api/trading/strategy", json={"enabled": False},
                          headers={**csrf(client), "Origin": "https://evil.example"})
    assert r.status_code == 403


async def test_kill_switch_endpoint_engages_even_without_engine(client):
    await login(client)
    r = await client.post("/api/trading/kill", json={"reason": "test"}, headers=csrf(client))
    assert r.status_code == 200
    st = (await client.get("/api/trading/state")).json()
    assert st["kill_switch"] and not st["strategy_enabled"]
    r = await client.post("/api/trading/strategy", json={"enabled": True}, headers=csrf(client))
    assert r.status_code == 409  # cannot re-arm while killed


async def test_strategy_config_validation_versioning_and_reset(client):
    await login(client)
    cfg = (await client.get("/api/strategy/config")).json()
    v1 = cfg["version"]
    bad = await client.put("/api/strategy/config", json={"params": {"max_leverage": 50}}, headers=csrf(client))
    assert bad.status_code == 422
    ok = await client.put("/api/strategy/config", json={"params": {"min_rr": 2.5}}, headers=csrf(client))
    assert ok.status_code == 200 and ok.json()["version"] == v1 + 1
    r = await client.post("/api/strategy/reset", headers=csrf(client))
    assert r.json()["params"]["min_rr"] == 2.0
    assert len((await client.get("/api/strategy/versions")).json()) >= 3


async def test_live_unlock_refused_until_ready(client):
    await login(client)
    r = await client.post("/api/trading/live/unlock", headers=csrf(client))
    assert r.status_code == 409  # no credentials configured in tests
    r = await client.post("/api/trading/live/enable", json={"token": "x", "confirmation": "ENABLE LIVE TRADING",
                                                            "acknowledge_risk": True}, headers=csrf(client))
    assert r.status_code == 403
    st = (await client.get("/api/trading/state")).json()
    assert st["mode"] == "paper"


async def test_readiness_checklist_lists_all_items(client):
    await login(client)
    r = (await client.get("/api/trading/readiness")).json()
    keys = {i["key"] for i in r["items"]}
    for k in ("binance_connected", "withdrawals_disabled", "ip_restricted", "paper_completed", "risk_reviewed",
              "stop_loss_tested", "kill_switch_tested", "backups_configured", "notifications_tested",
              "reconciliation_tested", "no_critical_errors", "two_factor"):
        assert k in keys
    assert r["ready"] is False


async def test_reauth_required_for_sensitive_actions(client):
    await login(client)
    async with client.store.factory() as s:  # expire the step-up window
        from sqlalchemy import update
        await s.execute(update(M.Session).values(reauth_at=utcnow().replace(year=2020)))
        await s.commit()
    r = await client.post("/api/trading/halt/clear", headers=csrf(client))
    assert r.status_code == 403 and r.json()["detail"]["reauth_required"]
    r = await client.post("/api/auth/reauth", json={"password": "nope-nope-nope"}, headers=csrf(client))
    assert r.status_code == 401
    r = await client.post("/api/auth/reauth", json={"password": PW}, headers=csrf(client))
    assert r.status_code == 200
    assert (await client.post("/api/trading/halt/clear", headers=csrf(client))).status_code == 200


async def test_totp_enrolment_and_login(client):
    await login(client)
    setup = (await client.post("/api/auth/totp/setup", headers=csrf(client))).json()
    assert "<svg" in setup["qr_svg"]
    bad = await client.post("/api/auth/totp/enable", json={"code": "000000"}, headers=csrf(client))
    assert bad.status_code == 400
    code = pyotp.TOTP(setup["secret"]).now()
    ok = await client.post("/api/auth/totp/enable", json={"code": code}, headers=csrf(client))
    assert ok.status_code == 200 and len(ok.json()["recovery_codes"]) == 8
    recovery = ok.json()["recovery_codes"][0]
    await client.post("/api/auth/logout", headers=csrf(client))
    client.cookies.clear()
    r = await login(client)
    assert r["json"] == {"totp_required": True}
    assert (await login(client, totp="123456"))["status"] == 401
    assert (await login(client, totp=recovery))["status"] == 200  # recovery code works once
    client.cookies.clear()
    assert (await login(client, totp=recovery))["status"] == 401


async def test_notifications_subscription_validation_and_test(client):
    await login(client)
    bad = await client.post("/api/notifications/subscribe", json={"endpoint": "http://x", "keys": {}},
                            headers=csrf(client))
    assert bad.status_code == 400
    ok = await client.post("/api/notifications/subscribe",
                           json={"endpoint": "https://web.push.apple.com/abc", "keys": {"p256dh": "k", "auth": "a"}},
                           headers=csrf(client))
    assert ok.status_code == 200
    r = (await client.post("/api/notifications/test", headers=csrf(client))).json()
    assert r["delivered"] == 0 and "VAPID" in (r["error"] or "")
    items = (await client.get("/api/notifications")).json()
    assert items["devices"] and items["items"]


async def test_performance_endpoint(client):
    await login(client)
    now = utcnow()
    for pnl in (50, -20, 30):
        await client.store.create_trade(mode="paper", symbol="BTCUSDT", direction="LONG", status="closed",
                                        realized_pnl=pnl, fees=1, opened_at=now, closed_at=now, quantity=0.01,
                                        equity_at_entry=10_000, r_multiple=pnl / 25)
    m = (await client.get("/api/performance?mode=paper&period=all")).json()
    assert m["trades"] == 3 and m["pnl"] == 60
    assert m["win_rate"] == pytest.approx(66.7) and m["profit_factor"] == 4.0


async def test_backtest_input_validation(client):
    await login(client)
    r = await client.post("/api/backtest", json={"start": "nonsense", "end": "2026-01-01"}, headers=csrf(client))
    assert r.status_code == 400
    r = await client.post("/api/backtest", json={"start": "2026-02-01", "end": "2026-01-01"}, headers=csrf(client))
    assert r.status_code == 400


async def test_logout_revokes_session(client):
    await login(client)
    token = client.cookies.get("kestrel_session")
    await client.post("/api/auth/logout", headers=csrf(client))
    client.cookies.set("kestrel_session", token)
    assert (await client.get("/api/auth/me")).status_code == 401


async def test_security_status_never_exposes_secrets(client):
    await login(client)
    body = (await client.get("/api/security/status")).text
    assert "t" * 48 not in body
