from __future__ import annotations

import pytest

from app.ai.provider import AIResult, StaticProvider, estimate_cost
from app.ai.schema import AIValidationError, validate_response
from app.ai.service import AIService, apply_policy
from app.core import params as P
from app.exchange.models import OrderRejected
from app.exchange.paper import PaperExchange, StaticPrices

SYM = "BTCUSDT"


def paper(price: float = 84_000.0, balance: float = 10_000.0, **kw) -> tuple[PaperExchange, StaticPrices]:  # noqa: ANN003
    pr = StaticPrices()
    pr.set(price, spread=0.2)
    return PaperExchange(pr, starting_balance=balance, **kw), pr


# ----------------------------------------------------------------------------- paper exchange
async def test_market_fill_applies_slippage_and_fee():
    ex, pr = paper()
    o = await ex.place_market_order(SYM, "BUY", 0.1, "e1")
    assert o.is_filled
    assert o.avg_price == pytest.approx(84_000.1 * (1 + 2 / 10_000))
    fee = 0.1 * o.avg_price * 0.0005
    acct = await ex.get_account()
    assert acct.wallet_balance == pytest.approx(10_000 - fee)
    pos = await ex.get_position(SYM)
    assert pos.direction == "LONG" and pos.size == pytest.approx(0.1)
    assert pos.margin == pytest.approx(0.1 * o.avg_price / 3)


async def test_stop_triggers_on_mark_and_gap_fills_worse():
    ex, pr = paper()
    await ex.place_market_order(SYM, "BUY", 0.1, "e1")
    await ex.place_stop_market(SYM, "SELL", 83_800, 0.1, "sl1")
    pr.set(83_900)
    assert await ex.on_market() == []
    pr.set(83_700)  # gapped through the stop
    ev = await ex.on_market()
    assert ev == ["filled:sl1"]
    fill = ex.state.fills[-1]
    assert fill.price < 83_700  # worse than trigger: gap + stop slippage
    assert await ex.get_position(SYM) is None
    assert fill.realized_pnl < 0


async def test_take_profit_partial_then_rest():
    ex, pr = paper()
    await ex.place_market_order(SYM, "BUY", 0.2, "e1")
    await ex.place_take_profit_market(SYM, "SELL", 84_300, 0.1, "tp1")
    await ex.place_take_profit_market(SYM, "SELL", 84_600, 0.1, "tp2")
    pr.set(84_350)
    await ex.on_market()
    assert (await ex.get_position(SYM)).size == pytest.approx(0.1)
    pr.set(84_650)
    await ex.on_market()
    assert await ex.get_position(SYM) is None
    assert ex.state.wallet_balance > 10_000


async def test_reduce_only_and_immediate_trigger_rejections():
    ex, pr = paper()
    with pytest.raises(OrderRejected):
        await ex.place_market_order(SYM, "SELL", 0.1, "x", reduce_only=True)  # nothing to reduce
    await ex.place_market_order(SYM, "BUY", 0.1, "e1")
    with pytest.raises(OrderRejected) as e:
        await ex.place_stop_market(SYM, "SELL", 84_100, 0.1, "sl")  # above mark → would trigger now
    assert e.value.code == -2021


async def test_insufficient_margin_and_duplicate_id():
    ex, pr = paper(balance=500)
    with pytest.raises(OrderRejected) as e:
        await ex.place_market_order(SYM, "BUY", 0.1, "e1")  # needs ~2800 margin
    assert e.value.code == -2019
    ex2, _ = paper()
    await ex2.place_market_order(SYM, "BUY", 0.01, "dup")
    with pytest.raises(OrderRejected):
        await ex2.place_market_order(SYM, "BUY", 0.01, "dup")


async def test_funding_settles_once_per_interval():
    ex, pr = paper()
    await ex.place_market_order(SYM, "BUY", 0.1, "e1")
    wallet = ex.state.wallet_balance
    t = 1_000_000
    await ex.on_market(funding_rate=0.0001, next_funding_ms=t, now_ms=t - 10)
    assert ex.state.wallet_balance == wallet
    await ex.on_market(funding_rate=0.0001, next_funding_ms=t + 28_800_000, now_ms=t + 5)  # interval rolled
    paid = wallet - ex.state.wallet_balance
    assert paid == pytest.approx(0.1 * 84_000 * 0.0001)
    await ex.on_market(funding_rate=0.0001, next_funding_ms=t + 28_800_000, now_ms=t + 50)
    assert wallet - ex.state.wallet_balance == pytest.approx(paid)  # no double charge


async def test_liquidation_when_mark_crosses():
    ex, pr = paper()
    await ex.place_market_order(SYM, "BUY", 0.1, "e1")
    liq = ex.liquidation_price()
    pr.set(liq - 10)
    ev = await ex.on_market()
    assert ev and ev[0].startswith("liquidated")
    assert await ex.get_position(SYM) is None
    assert ex.state.liquidations == 1


async def test_snapshot_restore_roundtrip():
    ex, pr = paper()
    await ex.place_market_order(SYM, "BUY", 0.1, "e1")
    await ex.place_stop_market(SYM, "SELL", 83_000, 0.1, "sl1")
    snap = ex.snapshot()
    ex2, _ = paper()
    ex2.prices = pr
    ex2.restore(snap)
    assert (await ex2.get_position(SYM)).size == pytest.approx(0.1)
    assert [o.client_order_id for o in await ex2.get_open_orders(SYM)] == ["sl1"]


# ----------------------------------------------------------------------------- AI validation
VALID = {"decision": "LONG", "confidence": 72, "market_regime": "trending_up", "reasoning": "Clean retest.",
         "risk_flags": [], "invalidation": "close below 83,400", "recommended_action": "ENTER"}


def test_valid_ai_response_passes():
    r = validate_response(dict(VALID))
    assert r.decision == "LONG" and r.confidence == 72


@pytest.mark.parametrize("mutate", [
    lambda d: d.pop("confidence"),
    lambda d: d.update(extra="x"),
    lambda d: d.update(decision="BUY"),
    lambda d: d.update(confidence=140),
    lambda d: d.update(confidence="high"),
    lambda d: d.update(risk_flags="none"),
    lambda d: d.update(recommended_action="YOLO"),
    lambda d: d.update(reasoning=""),
])
def test_invalid_ai_responses_rejected(mutate):  # noqa: ANN001
    d = dict(VALID)
    mutate(d)
    with pytest.raises(AIValidationError):
        validate_response(d)


def _res(**kw):  # noqa: ANN003, ANN202
    d = dict(VALID)
    d.update(kw)
    return AIResult(response=validate_response(d), raw=d, model="m")


def test_policy_matrix():
    p = P.defaults()
    assert apply_policy("off", "LONG", None, None, False, p).allowed
    assert apply_policy("required", "LONG", _res(), None, False, p).allowed
    assert not apply_policy("required", "LONG", _res(decision="SHORT"), None, False, p).allowed
    assert not apply_policy("required", "LONG", _res(decision="NO_TRADE"), None, False, p).allowed
    assert not apply_policy("required", "LONG", _res(confidence=40), None, False, p).allowed
    assert not apply_policy("required", "LONG", _res(recommended_action="SKIP"), None, False, p).allowed
    # unavailable
    assert not apply_policy("required", "LONG", None, "timeout", False, p).allowed
    p2 = dict(p, ai_unavailable_policy="deterministic")
    g = apply_policy("required", "LONG", None, "timeout", False, p2)
    assert g.allowed and g.degraded
    # budget
    assert not apply_policy("required", "LONG", None, None, True, p).allowed
    assert apply_policy("required", "LONG", None, None, True, dict(p, ai_budget_policy="deterministic")).allowed
    # advisory never gates
    assert apply_policy("advisory", "LONG", _res(decision="SHORT"), None, False, p).allowed


def test_cost_estimate_uses_pricing_and_overestimates_unknown():
    assert estimate_cost("gpt-6-sol", 1_000_000, 0, 0) == pytest.approx(2.0)
    assert estimate_cost("gpt-6-sol", 1_000_000, 1_000_000, 1_000_000) == pytest.approx(0.2 + 10.0)
    assert estimate_cost("some-future-model", 1_000_000, 0, 0) == pytest.approx(10.0)


async def test_ai_service_records_and_enforces_budget(store):
    prov = StaticProvider(dict(VALID))
    svc = AIService(prov, store)
    p = P.defaults()
    g = await svc.confirm_setup({"candidate": {"level": 1}}, "LONG", 123, p)
    assert g.allowed and g.analysis_id and g.used_ai
    # cache: same bar/level → no second provider call
    await svc.confirm_setup({"candidate": {"level": 1}}, "LONG", 123, p)
    assert len(prov.calls) == 1
    # budget exhausted
    p2 = dict(p, ai_daily_budget_usd=0.0)
    g2 = await svc.confirm_setup({"candidate": {"level": 2}}, "LONG", 124, p2)
    assert not g2.allowed and "budget" in g2.reason


async def test_ai_service_invalid_json_is_unavailable(store):
    bad = dict(VALID)
    bad["confidence"] = 999
    svc = AIService(StaticProvider(bad), store)
    g = await svc.confirm_setup({"candidate": {"level": 1}}, "LONG", 1, P.defaults())
    assert not g.allowed and g.degraded


async def test_ai_provider_crash_never_raises(store):
    class Boom(StaticProvider):
        async def analyze(self, *a, **k):  # noqa: ANN002, ANN003
            raise RuntimeError("provider bug")

    g = await AIService(Boom(), store).confirm_setup({}, "LONG", 1, P.defaults())
    assert not g.allowed
