"""Engine-level behaviour: kill switch, full decision pipeline, fail-safe halts."""

from __future__ import annotations

from collections import deque

import pytest
from sqlalchemy import select

from app.ai.provider import StaticProvider
from app.ai.service import AIService
from app.config import Settings
from app.db import models as M
from app.engine.runner import Engine
from app.strategy.breakout_retest import BreakoutRetestStrategy
from tests.fakes import FlakyVenue, Recorder, plan
from tests.helpers import breakout_retest_5m, extras, views_for

AGREE = {"decision": "LONG", "confidence": 80, "market_regime": "trending_up", "reasoning": "ok", "risk_flags": [],
         "invalidation": "x", "recommended_action": "ENTER"}


class FakeMarket:
    def __init__(self, price: float) -> None:
        self.errors: deque = deque()
        self.price = price
        self.funding_rate = 0.0001
        self.next_funding_ms = None
        self.open_interest = 80_000.0
        self.forming = {}

    def snapshot(self):  # noqa: ANN201
        return {"price": self.price, "mark_price": self.price, "spread_bps": 0.2}

    def oi_change_1h_pct(self):  # noqa: ANN201
        return 0.8

    def spread_bps(self):  # noqa: ANN201
        return 0.2

    def mark_price(self):  # noqa: ANN201
        return self.price

    def liquidations_1h(self):  # noqa: ANN201
        return {}


async def make_engine(store, venue: FlakyVenue, ai_response=None, live: FlakyVenue | None = None):  # noqa: ANN001, ANN201
    s = Settings(auth_secret="x" * 40)
    notes = Recorder()
    prov = StaticProvider(ai_response or AGREE)
    eng = Engine(s, store, notes, FakeMarket(venue.px.mark), None, AIService(prov, store), None)
    eng._salt = "t1"  # noqa: SLF001
    eng.trading = await store.trading_state()
    eng.cfg, eng.params = await store.active_params()
    eng.paper = venue
    eng.legs["paper"] = eng._make_leg(venue)  # noqa: SLF001
    if live is not None:
        eng.legs["live"] = eng._make_leg(live)  # noqa: SLF001
    for leg in eng.legs.values():
        async def _nosleep(_):  # noqa: ANN001, ANN202
            return None
        leg.execu._sleep = _nosleep  # noqa: SLF001
    return eng, notes, prov


# ----------------------------------------------------------------------------- kill switch
async def test_kill_switch_flattens_disables_and_logs(store):
    venue = FlakyVenue()
    eng, notes, _ = await make_engine(store, venue)
    res = await eng.legs["paper"].execu.open_trade(plan())
    assert res.ok
    out = await eng.kill("tester", "test")
    assert out["ok"]
    assert venue.state.position.quantity == 0
    assert venue.open_ids() == []  # protective orders cancelled only after flat
    st = await store.trading_state()
    assert st["kill_switch"] and not st["strategy_enabled"]
    t = await store.get_trade(res.trade_id)
    assert t.status == "closed"
    async with store.factory() as s:
        ev = (await s.execute(select(M.SystemEvent).where(M.SystemEvent.event == "kill_switch"))).scalars().all()
        trade_events = (await s.execute(select(M.TradeEvent).where(M.TradeEvent.trade_id == t.id))).scalars().all()
    assert ev and len(trade_events) > 5  # logs preserved
    assert any(c == "risk" and sev == "critical" for c, _, _, sev in notes.sent)
    ready = await store.get_setting("readiness")
    assert ready["kill_switch"]["ok"]


async def test_kill_switch_keep_protected_policy(store):
    venue = FlakyVenue()
    eng, *_ = await make_engine(store, venue)
    await store.set_setting("safety", {"kill_positions": "keep_protected"})
    res = await eng.legs["paper"].execu.open_trade(plan())
    t = await store.get_trade(res.trade_id)
    await eng.kill("tester", "test")
    assert venue.state.position.quantity == pytest.approx(0.1)
    assert t.protection["sl"] in venue.open_ids()  # still protected


async def test_kill_switch_in_live_returns_to_paper(store):
    paper_v, live_v = FlakyVenue(), FlakyVenue(mode="live")
    eng, *_ = await make_engine(store, paper_v, live=live_v)
    await store.update_trading({"mode": "live"})
    eng.trading = await store.trading_state()
    res = await eng.legs["live"].execu.open_trade(plan())
    assert res.ok
    await eng.kill("tester", "panic")
    assert live_v.state.position.quantity == 0
    assert (await store.trading_state())["mode"] == "paper"


# ----------------------------------------------------------------------------- pipeline
async def _setup_eval(eng):  # noqa: ANN001, ANN202
    v = views_for(breakout_retest_5m())
    ex = extras()
    ev = BreakoutRetestStrategy().evaluate(v, ex, eng.params)
    assert ev.setup is not None
    return ev, v, ex


async def test_pipeline_opens_paper_trade_when_everything_agrees(store):
    venue = FlakyVenue(price=83_470)
    eng, notes, prov = await make_engine(store, venue)
    ev, v, ex = await _setup_eval(eng)
    await eng.handle_setup(ev, v, ex)
    trades = await store.open_trades("paper")
    assert len(trades) == 1 and trades[0].ai_decision == "LONG"
    assert len(prov.calls) == 1
    async with store.factory() as s:
        sig = (await s.execute(select(M.Signal))).scalar_one()
    assert sig.status == "executed" and sig.trade_id == trades[0].id
    assert "signal" in notes.categories() and "trade_opened" in notes.categories()


async def test_ai_cannot_override_risk_rejection(store):
    venue = FlakyVenue(price=83_470)
    eng, _, prov = await make_engine(store, venue)
    await store.update_trading({"kill_switch": True})
    eng.trading = await store.trading_state()
    ev, v, ex = await _setup_eval(eng)
    await eng.handle_setup(ev, v, ex)
    assert prov.calls == []  # the model is never even asked
    assert await store.open_trades("paper") == []
    async with store.factory() as s:
        sig = (await s.execute(select(M.Signal))).scalar_one()
    assert sig.status == "rejected_risk"


async def test_ai_disagreement_blocks_trade(store):
    venue = FlakyVenue(price=83_470)
    eng, *_ = await make_engine(store, venue, ai_response={**AGREE, "decision": "NO_TRADE",
                                                           "recommended_action": "SKIP"})
    ev, v, ex = await _setup_eval(eng)
    await eng.handle_setup(ev, v, ex)
    assert await store.open_trades("paper") == []
    async with store.factory() as s:
        assert (await s.execute(select(M.Signal))).scalar_one().status == "rejected_ai"


async def test_ai_unavailable_default_policy_is_no_trade(store):
    venue = FlakyVenue(price=83_470)
    eng, *_ = await make_engine(store, venue, ai_response={"not": "valid"})
    ev, v, ex = await _setup_eval(eng)
    await eng.handle_setup(ev, v, ex)
    assert await store.open_trades("paper") == []
    async with store.factory() as s:
        assert (await s.execute(select(M.Signal))).scalar_one().status == "ai_unavailable"


async def test_risk_calculation_failure_halts(store):
    venue = FlakyVenue(price=83_470)
    eng, notes, _ = await make_engine(store, venue)
    venue.fail["get_account"] = (5, RuntimeError("corrupt response"))
    ev, v, ex = await _setup_eval(eng)
    await eng.handle_setup(ev, v, ex)
    st = await store.trading_state()
    assert st["halted"] and "risk calculation" in st["halt_reason"]
    assert await store.open_trades("paper") == []


async def test_daily_loss_limit_blocks_next_trade_and_alerts(store):
    venue = FlakyVenue(price=83_470)
    eng, notes, _ = await make_engine(store, venue)
    from app.db.types import utcnow
    await store.create_trade(mode="paper", symbol="BTCUSDT", direction="LONG", status="closed", realized_pnl=-250,
                             opened_at=utcnow(), closed_at=utcnow(), quantity=0.1)
    ev, v, ex = await _setup_eval(eng)
    await eng.handle_setup(ev, v, ex)
    assert await store.open_trades("paper") == []
    async with store.factory() as s:
        sig = (await s.execute(select(M.Signal))).scalar_one()
    assert sig.status == "rejected_risk" and "today" in sig.rejection
    t = (await store.recent_closed_trades("paper", 1))[0]
    await eng._on_trade_closed(t)  # noqa: SLF001
    assert any("Daily loss limit" in title for _, title, *_ in notes.sent)


async def test_halt_is_latching_and_idempotent(store):
    venue = FlakyVenue()
    eng, notes, _ = await make_engine(store, venue)
    await eng.halt("reason A")
    await eng.halt("reason A")
    assert len([n for n in notes.sent if n[1] == "Trading halted"]) == 1
    assert (await store.trading_state())["halted"]
    assert eng.gates().halted
