"""Binance credential safety verification.

Live trading is refused unless the configured key is valid AND safe:
* withdrawals disabled (``enableWithdrawals == false``)
* IP access restricted (``ipRestrict == true``)
* futures permission enabled
* account in one-way position mode (hedge mode is not supported)
* single-asset margin mode (isolated margin requires it)

Keys are only ever read from the environment; the database stores the status and
a 4-character hint, never the key or secret.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.config import Settings
from app.core.store import Store
from app.db import models as M
from app.db.types import utcnow
from app.exchange.binance_rest import BinanceFuturesAdapter
from app.exchange.models import AuthError, ExchangeError

log = logging.getLogger("kestrel.credentials")


def make_live_adapter(settings: Settings) -> BinanceFuturesAdapter | None:
    if not settings.binance_configured:
        return None
    return BinanceFuturesAdapter(
        api_key=settings.binance_api_key.get_secret_value(),
        api_secret=settings.binance_api_secret.get_secret_value(),
        base_url=settings.execution_rest_url,
        spot_url=settings.binance_spot_url,
        recv_window=settings.binance_recv_window_ms,
        testnet=settings.binance_testnet,
    )


async def check_credentials(settings: Settings, adapter: BinanceFuturesAdapter | None) -> dict[str, Any]:
    env = "testnet" if settings.binance_testnet else "mainnet"
    result: dict[str, Any] = {"environment": env, "status": "missing", "problems": [], "permissions": {},
                              "key_hint": None}
    if adapter is None:
        result["problems"].append("BINANCE_API_KEY / BINANCE_API_SECRET not set — paper trading only")
        return result
    result["key_hint"] = adapter.key_hint
    try:
        acct = await adapter.get_account()
        result["permissions"]["futures_account"] = True
        result["account"] = {"equity": acct.equity, "available": acct.available_balance}
    except AuthError as e:
        result["status"] = "invalid"
        result["problems"].append(f"credentials rejected: {e}")
        return result
    except ExchangeError as e:
        result["status"] = "error"
        result["problems"].append(f"could not reach Binance: {e}")
        return result
    problems: list[str] = result["problems"]
    try:
        dual = await adapter.position_mode_dual()
        result["permissions"]["hedge_mode"] = dual
        if dual:
            problems.append("account is in Hedge Mode — switch to One-way Mode")
        multi = await adapter.multi_assets_mode()
        result["permissions"]["multi_assets_mode"] = multi
        if multi:
            problems.append("Multi-Assets Mode is on — isolated margin requires Single-Asset Mode")
    except ExchangeError as e:
        problems.append(f"could not read position/margin mode: {e}")
    if settings.binance_testnet:
        result["permissions"]["restrictions"] = "not available on testnet (no real funds)"
    else:
        try:
            r = await adapter.api_restrictions()
            perms = {
                "enableWithdrawals": bool(r.get("enableWithdrawals")),
                "ipRestrict": bool(r.get("ipRestrict")),
                "enableFutures": bool(r.get("enableFutures")),
                "enableReading": bool(r.get("enableReading")),
                "enableInternalTransfer": bool(r.get("enableInternalTransfer")),
                "permitsUniversalTransfer": bool(r.get("permitsUniversalTransfer")),
            }
            result["permissions"].update(perms)
            if perms["enableWithdrawals"]:
                problems.append("WITHDRAWALS ARE ENABLED on this API key — disable them in Binance API Management")
            if not perms["ipRestrict"]:
                problems.append("API key is not IP-restricted — restrict it to your homelab's public IP")
            if not perms["enableFutures"]:
                problems.append("Futures permission is not enabled on this API key")
            if perms["permitsUniversalTransfer"] or perms["enableInternalTransfer"]:
                problems.append("transfer permissions are enabled — not needed, disable them")
        except ExchangeError as e:
            problems.append(f"could not verify key permissions: {e}")
    result["status"] = "unsafe" if problems else "connected"
    return result


async def store_credential_status(store: Store, result: dict[str, Any]) -> None:
    async with store.factory() as s:
        row = (await s.execute(select(M.ExchangeConnection).where(
            M.ExchangeConnection.venue == "binance",
            M.ExchangeConnection.environment == result["environment"]))).scalar_one_or_none()
        if row is None:
            row = M.ExchangeConnection(venue="binance", environment=result["environment"])
            s.add(row)
        row.status = result["status"]
        row.key_hint = result.get("key_hint")
        row.permissions = result.get("permissions", {})
        row.problems = result.get("problems", [])
        row.last_checked_at = utcnow()
        row.last_error = "; ".join(result.get("problems", []))[:2000] or None
        await s.commit()


async def credential_status(store: Store, settings: Settings) -> dict[str, Any]:
    env = "testnet" if settings.binance_testnet else "mainnet"
    async with store.factory() as s:
        row = (await s.execute(select(M.ExchangeConnection).where(
            M.ExchangeConnection.venue == "binance", M.ExchangeConnection.environment == env))).scalar_one_or_none()
    if row is None:
        return {"environment": env, "status": "missing" if not settings.binance_configured else "unchecked",
                "problems": [], "permissions": {}, "key_hint": None, "last_checked_at": None}
    return {"environment": env, "status": row.status, "problems": row.problems, "permissions": row.permissions,
            "key_hint": row.key_hint, "last_checked_at": row.last_checked_at.isoformat() if row.last_checked_at else None}
