from __future__ import annotations

import hashlib
import hmac
import json
import logging
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from app.config import Settings
from app.exchange.binance_rest import BinanceFuturesAdapter
from app.exchange.credentials import check_credentials
from app.exchange.models import AuthError, OrderRejected, RateLimited, UnknownOrderStatus
from app.logging_setup import JsonFormatter, RedactingFilter
from app.notifications.service import NotificationService

KEY, SECRET = "k" * 64, "s" * 64
EXINFO = {"symbols": [{"symbol": "BTCUSDT", "pricePrecision": 2, "quantityPrecision": 3, "filters": [
    {"filterType": "PRICE_FILTER", "tickSize": "0.10"}, {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "1000"},
    {"filterType": "MARKET_LOT_SIZE", "stepSize": "0.001", "minQty": "0.001", "maxQty": "120"},
    {"filterType": "MIN_NOTIONAL", "notional": "100"}]}]}


def adapter(handler) -> tuple[BinanceFuturesAdapter, list[httpx.Request]]:  # noqa: ANN001
    seen: list[httpx.Request] = []

    def wrap(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/fapi/v1/time":
            return httpx.Response(200, json={"serverTime": 1_790_000_000_000})
        if req.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=EXINFO)
        return handler(req)

    client = httpx.AsyncClient(transport=httpx.MockTransport(wrap))
    return BinanceFuturesAdapter(KEY, SECRET, "https://fapi.binance.com", client=client), seen


def _params(req: httpx.Request) -> dict[str, str]:
    return dict(parse_qsl(urlsplit(str(req.url)).query))


async def test_requests_are_signed_with_hmac_sha256():
    ad, seen = adapter(lambda r: httpx.Response(200, json={"totalMarginBalance": "100", "totalWalletBalance": "100",
                                                          "availableBalance": "90", "totalUnrealizedProfit": "0"}))
    acct = await ad.get_account()
    assert acct.available_balance == 90
    req = next(r for r in seen if r.url.path == "/fapi/v3/account")
    assert req.headers["X-MBX-APIKEY"] == KEY
    q = urlsplit(str(req.url)).query
    payload, sig = q.rsplit("&signature=", 1)
    assert sig == hmac.new(SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()
    assert "recvWindow=5000" in payload and "timestamp=" in payload


async def test_stop_loss_uses_algo_order_api_with_mark_price():
    def h(req: httpx.Request) -> httpx.Response:
        assert req.url.path == "/fapi/v1/algoOrder" and req.method == "POST"
        p = _params(req)
        return httpx.Response(200, json={"algoId": 1, "clientAlgoId": p["clientAlgoId"], "algoType": "CONDITIONAL",
                                         "orderType": p["type"], "symbol": "BTCUSDT", "side": p["side"],
                                         "quantity": p["quantity"], "algoStatus": "NEW",
                                         "triggerPrice": p["triggerPrice"], "reduceOnly": True})

    ad, seen = adapter(h)
    o = await ad.place_stop_market("BTCUSDT", "SELL", 83_800.04, 0.1234, "kst-1-SL1")
    p = _params(next(r for r in seen if r.url.path == "/fapi/v1/algoOrder"))
    assert p["algoType"] == "CONDITIONAL" and p["type"] == "STOP_MARKET"
    assert p["workingType"] == "MARK_PRICE" and p["reduceOnly"] == "true" and p["priceProtect"] == "true"
    assert p["triggerPrice"] == "83800" and p["quantity"] == "0.123"  # tick/step rounding (qty rounded down)
    assert o.conditional and o.is_open


async def test_order_503_is_unknown_status_not_failure():
    ad, _ = adapter(lambda r: httpx.Response(503, json={"code": -1, "msg": "Unknown error, please check your request"}))
    with pytest.raises(UnknownOrderStatus):
        await ad.place_market_order("BTCUSDT", "BUY", 0.01, "kst-1-E0")


async def test_order_timeout_is_unknown_status():
    def h(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=req)

    ad, _ = adapter(h)
    with pytest.raises(UnknownOrderStatus):
        await ad.place_market_order("BTCUSDT", "BUY", 0.01, "kst-1-E0")


async def test_unknown_order_query_returns_none():
    ad, _ = adapter(lambda r: httpx.Response(400, json={"code": -2013, "msg": "Order does not exist."}))
    assert await ad.get_order("BTCUSDT", "kst-1-E0", False) is None


async def test_error_mapping():
    ad, _ = adapter(lambda r: httpx.Response(401, json={"code": -2015, "msg": "Invalid API-key, IP, or permissions"}))
    with pytest.raises(AuthError):
        await ad.get_account()
    ad2, _ = adapter(lambda r: httpx.Response(429, json={"code": -1003, "msg": "Too many requests"}))
    with pytest.raises(RateLimited):
        await ad2.get_account()
    ad3, _ = adapter(lambda r: httpx.Response(400, json={"code": -2019, "msg": "Margin is insufficient."}))
    with pytest.raises(OrderRejected):
        await ad3.place_market_order("BTCUSDT", "BUY", 0.01, "x")


async def test_open_orders_merge_regular_and_algo():
    def h(req: httpx.Request) -> httpx.Response:
        if req.url.path == "/fapi/v1/openOrders":
            return httpx.Response(200, json=[{"symbol": "BTCUSDT", "clientOrderId": "a", "orderId": 1, "side": "BUY",
                                              "type": "LIMIT", "status": "NEW", "origQty": "0.1", "executedQty": "0"}])
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "clientAlgoId": "b", "algoId": 2, "side": "SELL",
                                          "orderType": "STOP_MARKET", "algoStatus": "NEW", "quantity": "0.1",
                                          "triggerPrice": "80000"}])

    ad, _ = adapter(h)
    orders = await ad.get_open_orders("BTCUSDT")
    assert {o.client_order_id for o in orders} == {"a", "b"}
    assert next(o for o in orders if o.client_order_id == "b").conditional


@pytest.mark.parametrize("perms,expect", [
    ({"enableWithdrawals": False, "ipRestrict": True, "enableFutures": True}, "connected"),
    ({"enableWithdrawals": True, "ipRestrict": True, "enableFutures": True}, "unsafe"),
    ({"enableWithdrawals": False, "ipRestrict": False, "enableFutures": True}, "unsafe"),
])
async def test_credential_safety_check(perms, expect):  # noqa: ANN001
    def h(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path == "/fapi/v3/account":
            return httpx.Response(200, json={"totalMarginBalance": "100", "totalWalletBalance": "100",
                                             "availableBalance": "100", "totalUnrealizedProfit": "0"})
        if path == "/fapi/v1/positionSide/dual":
            return httpx.Response(200, json={"dualSidePosition": False})
        if path == "/fapi/v1/multiAssetsMargin":
            return httpx.Response(200, json={"multiAssetsMargin": False})
        if path == "/sapi/v1/account/apiRestrictions":
            return httpx.Response(200, json=perms)
        return httpx.Response(404)

    ad, _ = adapter(h)
    res = await check_credentials(Settings(auth_secret="x" * 40), ad)
    assert res["status"] == expect
    if perms["enableWithdrawals"]:
        assert any("WITHDRAWALS" in p for p in res["problems"])


async def test_missing_credentials_status():
    res = await check_credentials(Settings(auth_secret="x" * 40), None)
    assert res["status"] == "missing"


# ----------------------------------------------------------------------------- notifications
class FakeSender:
    configured = True

    def __init__(self, codes: list[int]) -> None:
        self.codes = codes
        self.payloads: list[dict] = []

    async def send(self, sub, payload, ttl, urgency):  # noqa: ANN001, ANN201
        self.payloads.append(payload)
        code = self.codes.pop(0) if self.codes else 201
        return (code < 300), code, None if code < 300 else "gone"


async def _sub(store, endpoint="https://web.push.apple.com/x"):  # noqa: ANN001, ANN202
    from app.db import models as M
    async with store.factory() as s:
        u = M.User(username=f"u{endpoint[-3:]}", password_hash="x")
        s.add(u)
        await s.flush()
        s.add(M.PushSubscription(user_id=u.id, endpoint=endpoint, p256dh="k", auth="a"))
        await s.commit()


async def test_notification_prefs_dedupe_and_critical_override(store):
    await _sub(store)
    snd = FakeSender([])
    svc = NotificationService(store, snd)
    await store.set_setting("notifications", {"signal": False})
    await svc.notify("signal", "t", "b")
    assert snd.payloads == []  # disabled category
    await svc.notify("signal", "t2", "b", "critical")
    assert len(snd.payloads) == 1  # critical always delivered
    await svc.notify("trade_opened", "BTC LONG opened", "BTC LONG opened at $84,750")
    await svc.notify("trade_opened", "BTC LONG opened", "BTC LONG opened at $84,750")
    assert len(snd.payloads) == 2  # duplicate suppressed
    assert snd.payloads[-1]["body"] == "BTC LONG opened at $84,750"


async def test_gone_subscriptions_are_pruned(store):
    from sqlalchemy import func, select

    from app.db import models as M
    await _sub(store, "https://web.push.apple.com/aaa")
    await _sub(store, "https://web.push.apple.com/bbb")
    svc = NotificationService(store, FakeSender([201, 410]))
    await svc.notify("system", "x", "y")
    async with store.factory() as s:
        n = (await s.execute(select(func.count()).select_from(M.PushSubscription))).scalar()
        row = (await s.execute(select(M.Notification))).scalars().first()
    assert n == 1 and row.delivered == 1 and row.failed == 1


# ----------------------------------------------------------------------------- logging
def test_log_redaction_of_secrets_and_patterns():
    f = RedactingFilter(["supersecretvalue123"])
    rec = logging.LogRecord("x", logging.INFO, __file__, 1,
                            "call ?a=1&signature=" + "ab" * 32 + " key sk-proj-ABCDEFGHIJKLMNOPQRSTUVWX pw supersecretvalue123",
                            None, None)
    rec.extra_field = {"header": "X-MBX-APIKEY: " + "Z" * 40}
    f.filter(rec)
    out = JsonFormatter().format(rec)
    assert "supersecretvalue123" not in out and "ab" * 32 not in out
    assert "ABCDEFGHIJKLMNOPQRSTUVWX" not in out and "Z" * 40 not in out
    assert json.loads(out)["msg"].count("[REDACTED]") >= 3
