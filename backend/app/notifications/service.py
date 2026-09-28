"""Web Push notifications (VAPID) — works with iOS/iPadOS 16.4+ Home Screen web apps.

* Categories can be switched off individually; ``critical`` severity always goes out.
* Identical notifications within 5 minutes are suppressed (recorded, not sent).
* Subscriptions answering 404/410 are pruned (the browser revoked them).
* Delivery failures never propagate into trading code.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any

from sqlalchemy import delete, select

from app.config import Settings
from app.core.store import Store
from app.db import models as M
from app.db.types import utcnow

log = logging.getLogger("kestrel.notify")

CATEGORIES = {
    "trade_opened": "Trade opened",
    "stop_loss": "Stop-loss hit",
    "take_profit": "Take-profit hit",
    "trade_closed": "Trade closed (other)",
    "signal": "Setup detected",
    "risk": "Risk limits / emergencies",
    "system": "System health",
    "live_mode": "Live mode changes",
    "ai": "AI analyses",
}

DEDUPE_SECONDS = 300


class PushSender:
    """Thin wrapper so tests can replace delivery."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @property
    def configured(self) -> bool:
        return self.settings.vapid_configured

    async def send(self, sub: M.PushSubscription, payload: dict[str, Any], ttl: int, urgency: str) -> tuple[bool, int | None, str | None]:
        from pywebpush import WebPushException, webpush

        info = {"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}}

        def _do() -> tuple[bool, int | None, str | None]:
            try:
                resp = webpush(subscription_info=info, data=json.dumps(payload),
                               vapid_private_key=self.settings.vapid_private_key.get_secret_value(),
                               vapid_claims={"sub": self.settings.vapid_subject}, ttl=ttl,
                               headers={"Urgency": urgency}, timeout=10)
                code = getattr(resp, "status_code", 201)
                return (200 <= code < 300), code, None
            except WebPushException as e:
                code = getattr(getattr(e, "response", None), "status_code", None)
                return False, code, str(e)[:300]
            except Exception as e:  # noqa: BLE001
                return False, None, f"{type(e).__name__}: {str(e)[:200]}"

        return await asyncio.to_thread(_do)


class NotificationService:
    def __init__(self, store: Store, sender: PushSender):
        self.store = store
        self.sender = sender
        self._recent: dict[str, float] = {}
        self.last_error: str | None = None
        self.last_success_at: float | None = None

    async def notify(self, category: str, title: str, body: str, severity: str = "info",
                     data: dict[str, Any] | None = None) -> None:
        try:
            await self._notify(category, title, body, severity, data or {})
        except Exception:  # noqa: BLE001
            log.exception("notification pipeline failed")

    async def _notify(self, category: str, title: str, body: str, severity: str, data: dict[str, Any]) -> M.Notification:
        prefs = await self.store.notify_prefs()
        key = f"{category}|{title}|{body}"
        now = time.time()
        self._recent = {k: t for k, t in self._recent.items() if now - t < DEDUPE_SECONDS}
        suppressed = False
        reason = None
        if key in self._recent and not data.get("force"):
            suppressed, reason = True, "duplicate within 5 min"
        elif not prefs.get(category, True) and severity != "critical" and not data.get("force"):
            suppressed, reason = True, "category disabled"
        self._recent[key] = now
        async with self.store.factory() as s:
            n = M.Notification(category=category, title=title[:200], body=body, severity=severity,
                               suppressed=suppressed, error=reason, data={k: v for k, v in data.items() if k != "force"})
            s.add(n)
            await s.commit()
            await s.refresh(n)
        if suppressed:
            return n
        if not self.sender.configured:
            await self._update(n.id, error="web push not configured (VAPID keys missing)")
            return n
        async with self.store.factory() as s:
            subs = list((await s.execute(select(M.PushSubscription))).scalars().all())
        if not subs:
            await self._update(n.id, error="no subscribed devices")
            return n
        payload = {
            "title": title, "body": body, "category": category, "severity": severity,
            "url": data.get("url", "/dashboard"), "tag": f"{category}-{data.get('trade_id', n.id)}",
            "id": n.id, "ts": int(now * 1000),
        }
        ttl = 86_400 if severity == "critical" else 3_600
        urgency = "high" if severity in ("critical", "warning") or category in ("trade_opened", "stop_loss",
                                                                               "take_profit") else "normal"
        results = await asyncio.gather(*(self.sender.send(sub, payload, ttl, urgency) for sub in subs))
        delivered = failed = 0
        errors: list[str] = []
        dead: list[int] = []
        async with self.store.factory() as s:
            for sub, (ok, code, err) in zip(subs, results, strict=True):
                row = await s.get(M.PushSubscription, sub.id)
                if row is None:
                    continue
                if ok:
                    delivered += 1
                    row.last_success_at = utcnow()
                    row.failure_count = 0
                else:
                    failed += 1
                    errors.append(f"{code}: {err}")
                    row.failure_count += 1
                    if code in (404, 410) or row.failure_count >= 10:
                        dead.append(row.id)
            if dead:
                await s.execute(delete(M.PushSubscription).where(M.PushSubscription.id.in_(dead)))
            await s.commit()
        if delivered:
            self.last_success_at = now
            self.last_error = None
        elif errors:
            self.last_error = errors[0]
        await self._update(n.id, delivered=delivered, failed=failed, error="; ".join(errors)[:1000] or None)
        return n

    async def _update(self, nid: int, **fields: Any) -> None:
        async with self.store.factory() as s:
            n = await s.get(M.Notification, nid)
            if n:
                for k, v in fields.items():
                    setattr(n, k, v)
                await s.commit()

    async def send_test(self) -> M.Notification:
        n = await self._notify("system", "Kestrel test notification",
                               "Notifications are working. You will be alerted about trades, risk events and "
                               "system problems.", "info", {"force": True, "url": "/notifications"})
        return n
