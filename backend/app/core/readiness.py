"""Live-trading readiness checklist. Live mode cannot be unlocked unless every required item passes."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select

from app.config import Settings
from app.core.store import Store
from app.db import models as M
from app.db.types import utcnow
from app.exchange.credentials import credential_status


def _recent(entry: dict[str, Any] | None, days: float) -> tuple[bool, str]:
    if not entry:
        return False, "never done"
    try:
        at = datetime.fromisoformat(entry["at"])
    except (KeyError, ValueError):
        return False, "never done"
    age = utcnow() - at
    if not entry.get("ok"):
        return False, f"last attempt failed ({entry.get('detail') or 'see system log'})"
    if age > timedelta(days=days):
        return False, f"last done {age.days} days ago (must be within {days:g} days)"
    return True, f"done {at:%Y-%m-%d %H:%M} UTC"


async def compute_readiness(store: Store, settings: Settings, user: M.User | None = None) -> dict[str, Any]:
    items: list[dict[str, Any]] = []

    def add(key: str, label: str, ok: bool, detail: str, how: str, required: bool = True) -> None:
        items.append({"key": key, "label": label, "ok": bool(ok), "detail": detail, "how": how, "required": required})

    cred = await credential_status(store, settings)
    perms = cred.get("permissions") or {}
    testnet = settings.binance_testnet
    add("binance_connected", "Binance API connected", cred["status"] == "connected",
        f"status: {cred['status']}" + (f" — {'; '.join(cred['problems'])}" if cred.get("problems") else ""),
        "Set BINANCE_API_KEY / BINANCE_API_SECRET in .env, restart, then Security → Verify now.")
    add("withdrawals_disabled", "Withdrawal permission disabled",
        testnet or perms.get("enableWithdrawals") is False,
        "testnet (no real funds)" if testnet else
        {False: "withdrawals disabled", True: "WITHDRAWALS ENABLED"}.get(perms.get("enableWithdrawals"), "not verified"),
        "Binance → API Management → edit key → untick 'Enable Withdrawals'.")
    add("ip_restricted", "IP restriction configured", testnet or perms.get("ipRestrict") is True,
        "testnet" if testnet else ("restricted to trusted IPs" if perms.get("ipRestrict") else "unrestricted"),
        "Binance → API Management → 'Restrict access to trusted IPs only' → your homelab's public IP.")
    add("account_mode", "One-way, single-asset margin", perms.get("hedge_mode") is False and
        perms.get("multi_assets_mode") is False, f"hedge={perms.get('hedge_mode')} multi-asset={perms.get('multi_assets_mode')}",
        "Binance Futures → Preferences → Position Mode: One-way; Asset Mode: Single-Asset.")

    safety = await store.safety()
    async with store.factory() as s:
        n_paper = (await s.execute(select(func.count()).select_from(M.Trade).where(
            M.Trade.mode == "paper", M.Trade.status == "closed", M.Trade.origin == "strategy"))).scalar() or 0
        first = (await s.execute(select(func.min(M.Trade.opened_at)).where(M.Trade.mode == "paper"))).scalar()
        backup = await s.get(M.ComponentStatus, "backup")
        engine = await s.get(M.ComponentStatus, "engine")
        critical = (await s.execute(select(func.count()).select_from(M.SystemEvent).where(
            M.SystemEvent.level == "critical", M.SystemEvent.resolved.is_(False),
            M.SystemEvent.ts >= utcnow() - timedelta(hours=24)))).scalar() or 0
    days = (utcnow() - first).days if first else 0
    need_n, need_d = safety["min_paper_trades_for_live"], safety["min_paper_days_for_live"]
    add("paper_completed", "Paper trading completed", n_paper >= need_n and days >= need_d,
        f"{n_paper}/{need_n} closed paper trades over {days}/{need_d} days",
        "Let the strategy run in paper mode; thresholds are in Settings → Safety.")
    ready = await store.get_setting("readiness", {})
    ok, d = _recent(ready.get("risk_review"), 30)
    add("risk_reviewed", "Risk limits reviewed", ok, d, "Strategy → review the risk section → 'I have reviewed these limits'.")
    ok, d = _recent(ready.get("protection_test"), 30)
    add("stop_loss_tested", "Stop-loss placement tested on Binance", ok, d,
        "Security → 'Run stop-loss self-test' (places and cancels a far-away reduce-only stop).")
    ok, d = _recent(ready.get("kill_switch"), 30)
    add("kill_switch_tested", "Kill switch tested", ok, d, "Press KILL SWITCH once (in paper mode), then re-arm.")
    b_ok = backup is not None and backup.status == "ok" and backup.updated_at > utcnow() - timedelta(hours=36)
    add("backups_configured", "Database backups running", b_ok,
        backup.detail if backup else "no backup has reported yet",
        "The 'backup' container dumps the database daily; check `docker compose logs backup`.")
    ok, d = _recent(ready.get("notification_test"), 30)
    add("notifications_tested", "Notifications tested", ok, d, "Notifications → Enable → Send test notification.")
    ok, d = _recent(ready.get("reconciliation"), 2)
    add("reconciliation_tested", "Reconciliation tested", ok, d,
        "Security → 'Run live reconciliation check' (read-only unless unknown positions exist).")
    add("two_factor", "Two-factor authentication enabled", bool(user and user.totp_enabled),
        "enabled" if user and user.totp_enabled else "disabled", "Security → Two-factor authentication.")
    st = await store.trading_state()
    add("no_critical_errors", "No critical errors", critical == 0 and not st.get("halted"),
        "none in 24 h" if critical == 0 and not st.get("halted") else
        f"{critical} unresolved critical event(s)" + (f"; halted: {st.get('halt_reason')}" if st.get("halted") else ""),
        "System → review the events → Acknowledge (or Trading → Clear halt).")
    eng_ok = engine is not None and engine.updated_at > utcnow() - timedelta(seconds=45)
    add("engine_running", "Trading engine running", eng_ok, engine.detail if engine else "no heartbeat",
        "docker compose ps / logs engine")
    live = await store.live_state(["engine"])
    skew = live.get("engine", {}).get("clock_skew_ms")
    add("clock_synced", "Clock synchronised with Binance", skew is not None and abs(skew) < safety["max_clock_skew_ms"],
        f"skew {skew} ms" if skew is not None else "unknown", "Enable NTP on the host (timedatectl set-ntp true).")
    all_ok = all(i["ok"] for i in items if i["required"])
    return {"ready": all_ok, "items": items, "passed": sum(1 for i in items if i["ok"]), "total": len(items)}
