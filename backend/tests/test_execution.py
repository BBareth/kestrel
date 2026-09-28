"""Execution engine, position monitor and reconciliation — including failure injection."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.db import models as M
from app.exchange.models import ExchangeError, OrderRejected, UnknownOrderStatus
from tests.fakes import SAFETY, SYM, FlakyVenue, plan, rig


async def events(store, trade_id):  # noqa: ANN001, ANN201
    async with store.factory() as s:
        rows = (await s.execute(select(M.TradeEvent).where(M.TradeEvent.trade_id == trade_id)
                                .order_by(M.TradeEvent.id))).scalars().all()
        return [r.step for r in rows]


# ----------------------------------------------------------------------------- entry
async def test_open_trade_places_and_verifies_protection(store):
    venue, x, *_ , notes, halts = rig(store)
    res = await x.open_trade(plan())
    assert res.ok, res.message
    t = await store.get_trade(res.trade_id)
    assert t.status == "open" and t.quantity == pytest.approx(0.1)
    assert set(t.protection) >= {"sl", "tp1", "tp2"}
    assert sorted(venue.open_ids()) == sorted([t.protection["sl"], t.protection["tp1"], t.protection["tp2"]])
    steps = await events(store, t.id)
    for step in ("validated", "order_submitted", "order_filled", "sl_submitted", "tp1_submitted", "tp2_submitted",
                 "protected"):
        assert step in steps
    assert "trade_opened" in notes.categories()
    assert not halts.reasons


async def test_pre_events_form_complete_audit_trail(store):
    _, x, *_ = rig(store)
    p = plan(pre_events=[("signal_generated", "s", {}), ("strategy_approved", "a", {}), ("risk_approved", "r", {}),
                         ("ai_analyzed", "ai", {})])
    res = await x.open_trade(p)
    steps = await events(store, res.trade_id)
    assert steps[:4] == ["signal_generated", "strategy_approved", "risk_approved", "ai_analyzed"]


async def test_slippage_guard_aborts_before_any_order(store):
    venue, x, *_ = rig(store, FlakyVenue(price=84_120))
    res = await x.open_trade(plan())
    assert not res.ok and "moved" in res.message
    assert res.trade_id is None and venue.state.position.quantity == 0
    assert "place_market_order" not in venue.calls


@pytest.mark.parametrize("setup_dup", ["venue_position", "db_trade"])
async def test_duplicate_prevention(store, setup_dup):  # noqa: ANN001
    venue, x, *_ = rig(store)
    if setup_dup == "venue_position":
        await venue.place_market_order(SYM, "BUY", 0.01, "manual-1")
    else:
        await store.create_trade(mode="paper", symbol=SYM, direction="LONG", status="open", quantity=0.1)
    res = await x.open_trade(plan())
    assert not res.ok and "duplicate" in res.message
    assert venue.calls.count("place_market_order") <= (1 if setup_dup == "venue_position" else 0)


async def test_insufficient_margin_rejected(store):
    venue, x, *_ = rig(store, FlakyVenue(balance=500))
    res = await x.open_trade(plan())
    assert not res.ok and "margin" in res.message


async def test_price_beyond_stop_rejected(store):
    _, x, *_ = rig(store, FlakyVenue(price=83_790))
    res = await x.open_trade(plan(max_slippage_bps=100))
    assert not res.ok


async def test_entry_unknown_status_with_no_order_means_no_trade(store):
    venue, x, *_ = rig(store)
    venue.fail["place_market_order"] = (1, UnknownOrderStatus("timeout"))
    res = await x.open_trade(plan())
    assert not res.ok and res.trade_id
    assert (await store.get_trade(res.trade_id)).status == "cancelled"
    assert venue.state.position.quantity == 0


async def test_entry_unknown_status_but_executed_is_resolved_without_duplicate(store):
    venue, x, *_ = rig(store)
    venue.ghost["place_market_order"] = 1
    res = await x.open_trade(plan())
    assert res.ok, res.message
    assert venue.state.position.quantity == pytest.approx(0.1)  # exactly one entry
    assert venue.calls.count("place_market_order") == 1


async def test_entry_rejected_by_venue(store):
    venue, x, *_ = rig(store)
    venue.fail["place_market_order"] = (1, OrderRejected("bad", code=-1111))
    res = await x.open_trade(plan())
    assert not res.ok and (await store.get_trade(res.trade_id)).status == "cancelled"


# ----------------------------------------------------------------------------- protection failures
async def test_stop_loss_failure_flattens_halts_and_alerts(store):
    venue, x, _, _, notes, halts = rig(store)
    venue.fail["place_stop_market"] = (99, OrderRejected("rejected", code=-1111))
    res = await x.open_trade(plan())
    assert not res.ok and res.emergency
    assert venue.state.position.quantity == 0  # never left unprotected
    assert halts.reasons and "stop-loss" in halts.reasons[0]
    t = await store.get_trade(res.trade_id)
    assert t.status == "closed" and t.exit_reason == "emergency"
    assert any(sev == "critical" for *_, sev in notes.sent)
    assert "emergency" in await events(store, t.id)


async def test_stop_loss_unknown_status_resolved_no_duplicate_stop(store):
    venue, x, *_ = rig(store)
    venue.ghost["place_stop_market"] = 1
    res = await x.open_trade(plan())
    assert res.ok, res.message
    stops = [o for o in venue.state.orders.values() if o.type == "STOP_MARKET"]
    assert len(stops) == 1


async def test_stop_loss_transient_failure_retried(store):
    venue, x, *_ = rig(store)
    venue.fail["place_stop_market"] = (1, ExchangeError("502"))
    res = await x.open_trade(plan())
    assert res.ok
    assert len([o for o in venue.state.orders.values() if o.type == "STOP_MARKET"]) == 1


async def test_tp_failure_close_policy(store):
    venue, x, _, _, notes, halts = rig(store)
    venue.fail["place_take_profit_market"] = (99, OrderRejected("no", code=-1111))
    res = await x.open_trade(plan(tp_failure_policy="close"))
    assert not res.ok
    assert venue.state.position.quantity == 0
    assert not halts.reasons
    assert (await store.get_trade(res.trade_id)).status == "closed"


async def test_tp_failure_keep_policy_uses_software_tp(store):
    venue, x, mon, *_ = rig(store)
    venue.fail["place_take_profit_market"] = (99, OrderRejected("no", code=-1111))
    res = await x.open_trade(plan(tp_failure_policy="keep"))
    assert res.ok
    t = await store.get_trade(res.trade_id)
    assert t.protection.get("software_tp") and t.protection.get("sl") in venue.open_ids()
    venue.fail.clear()
    venue.px.set(84_320)
    await mon.run_once(SAFETY)
    assert (await store.get_trade(t.id)).status == "closed"


async def test_move_stop_places_new_before_cancelling_old(store):
    venue, x, *_ = rig(store)
    res = await x.open_trade(plan())
    t = await store.get_trade(res.trade_id)
    old = t.protection["sl"]
    order_of_calls_before = len(venue.calls)
    assert await x.move_stop(t, 83_900, 0.1, "test")
    t = await store.get_trade(t.id)
    assert t.protection["sl"] != old and t.stop_price == 83_900
    assert old not in venue.open_ids() and t.protection["sl"] in venue.open_ids()
    seq = venue.calls[order_of_calls_before:]
    assert seq.index("place_stop_market") < seq.index("cancel_order")


async def test_failed_stop_move_keeps_old_stop(store):
    venue, x, *_ = rig(store)
    res = await x.open_trade(plan())
    t = await store.get_trade(res.trade_id)
    old = t.protection["sl"]
    venue.fail["place_stop_market"] = (99, OrderRejected("no", code=-1111))
    assert not await x.move_stop(t, 83_900, 0.1, "test")
    assert old in venue.open_ids()


# ----------------------------------------------------------------------------- monitor
async def test_stop_loss_hit_settles_pnl_and_cleans_up(store):
    venue, x, mon, _, notes, _ = rig(store)
    res = await x.open_trade(plan())
    venue.px.set(83_750)
    await venue.on_market()
    await mon.run_once(SAFETY)
    t = await store.get_trade(res.trade_id)
    assert t.status == "closed" and t.exit_reason == "stop_loss"
    assert t.realized_pnl < 0 and t.fees > 0
    assert t.realized_pnl == pytest.approx(t.gross_pnl - t.fees + t.funding)
    assert t.r_multiple is not None and t.r_multiple < -0.9
    assert venue.open_ids() == []  # leftover TPs cancelled
    assert "stop_loss" in notes.categories()


async def test_tp1_then_breakeven_then_tp2(store):
    venue, x, mon, _, notes, _ = rig(store)
    res = await x.open_trade(plan())
    venue.px.set(84_310)
    await venue.on_market()
    await mon.run_once(SAFETY)
    t = await store.get_trade(res.trade_id)
    assert t.tp1_filled and t.remaining_qty == pytest.approx(0.05)
    assert t.stop_price > t.entry_price  # breakeven + fees
    assert "take_profit" in notes.categories()
    venue.px.set(84_620)
    await venue.on_market()
    await mon.run_once(SAFETY)
    t = await store.get_trade(res.trade_id)
    assert t.status == "closed" and t.exit_reason == "take_profit" and t.realized_pnl > 0


async def test_breakeven_stop_after_tp1(store):
    venue, x, mon, *_ = rig(store)
    res = await x.open_trade(plan())
    venue.px.set(84_310)
    await venue.on_market()
    await mon.run_once(SAFETY)
    venue.px.set(84_000)
    await venue.on_market()
    await mon.run_once(SAFETY)
    t = await store.get_trade(res.trade_id)
    assert t.status == "closed" and t.exit_reason == "breakeven_stop"


async def test_missing_stop_is_restored(store):
    venue, x, mon, _, notes, halts = rig(store)
    res = await x.open_trade(plan())
    t = await store.get_trade(res.trade_id)
    venue.external_cancel(t.protection["sl"])
    out = await mon.run_once(SAFETY)
    assert out["protection"] == "repaired"
    t = await store.get_trade(t.id)
    assert t.protection["sl"] in venue.open_ids()
    assert not halts.reasons


async def test_unrestorable_stop_flattens_and_halts(store):
    venue, x, mon, _, notes, halts = rig(store)
    res = await x.open_trade(plan())
    t = await store.get_trade(res.trade_id)
    venue.external_cancel(t.protection["sl"])
    venue.fail["place_stop_market"] = (99, OrderRejected("no", code=-1111))
    await mon.run_once(SAFETY)
    assert venue.state.position.quantity == 0
    assert halts.reasons
    assert any("EMERGENCY" in title for _, title, *_ in notes.sent)


async def test_flatten_failure_leaves_protection_in_place(store):
    venue, x, *_ = rig(store)
    res = await x.open_trade(plan())
    t = await store.get_trade(res.trade_id)
    venue.fail["place_market_order"] = (99, ExchangeError("down"))
    ok = await x.close_trade(t, "manual close")
    assert not ok
    t = await store.get_trade(t.id)
    assert t.status == "open"
    assert t.protection["sl"] in venue.open_ids()  # stop NOT cancelled
    async with store.factory() as s:
        crit = (await s.execute(select(M.SystemEvent).where(M.SystemEvent.level == "critical"))).scalars().all()
    assert crit


async def test_external_close_detected(store):
    venue, x, mon, *_ = rig(store)
    res = await x.open_trade(plan())
    await venue.place_market_order(SYM, "SELL", 0.1, "user-manual", reduce_only=True)
    await mon.run_once(SAFETY)
    t = await store.get_trade(res.trade_id)
    assert t.status == "closed" and t.exit_reason == "external"


async def test_monitor_survives_exchange_outage(store):
    venue, x, mon, *_ = rig(store)
    res = await x.open_trade(plan())
    venue.down = True
    with pytest.raises(ExchangeError):
        await mon.run_once(SAFETY)
    t = await store.get_trade(res.trade_id)
    assert t.status == "open"  # nothing changed while blind; venue-side stop still protects
    venue.down = False
    venue.px.set(83_700)
    await venue.on_market()  # stop fires on the venue during/after the outage
    await mon.run_once(SAFETY)
    assert (await store.get_trade(t.id)).exit_reason == "stop_loss"


# ----------------------------------------------------------------------------- reconciliation
async def test_restart_with_protected_position_places_nothing(store):
    venue, x, *_ = rig(store)
    res = await x.open_trade(plan())
    before = sorted(venue.open_ids())
    _, _, _, rec2, _, halts = rig(store, venue)  # fresh engine objects = process restart
    report = await rec2.run(SAFETY)
    assert sorted(venue.open_ids()) == before
    assert any("protection ok" in a for a in report["actions"])
    assert not halts.reasons
    assert (await store.get_trade(res.trade_id)).status == "open"


async def test_restart_after_stop_hit_while_offline(store):
    venue, x, *_ = rig(store)
    res = await x.open_trade(plan())
    venue.px.set(83_700)
    await venue.on_market()
    _, _, _, rec2, *_ = rig(store, venue)
    await rec2.run(SAFETY)
    t = await store.get_trade(res.trade_id)
    assert t.status == "closed" and t.exit_reason == "stop_loss"


async def test_pending_entry_filled_before_crash_gets_protected(store):
    venue, x, _, rec, *_ = rig(store)
    t = await store.create_trade(mode="paper", symbol=SYM, direction="LONG", status="pending", quantity=0.1,
                                 stop_price=83_800, initial_stop=83_800, tp1_price=84_300, tp2_price=84_600,
                                 protection={"seq": 0})
    await venue.place_market_order(SYM, "BUY", 0.1, x.cid(t.id, "E"))
    report = await rec.run(SAFETY)
    t = await store.get_trade(t.id)
    assert t.status == "open"
    assert t.protection.get("sl") in venue.open_ids()
    assert any("had filled" in a for a in report["actions"])


async def test_pending_entry_never_filled_is_cancelled(store):
    venue, x, _, rec, *_ = rig(store)
    t = await store.create_trade(mode="paper", symbol=SYM, direction="LONG", status="pending", quantity=0.1)
    await rec.run(SAFETY)
    assert (await store.get_trade(t.id)).status == "cancelled"
    assert venue.state.position.quantity == 0


@pytest.mark.parametrize("policy", ["protect", "flatten", "halt_only"])
async def test_unexpected_position_policies(store, policy):  # noqa: ANN001
    venue, x, _, rec, notes, halts = rig(store)
    await venue.place_market_order(SYM, "BUY", 0.05, "someone-else")
    await rec.run({**SAFETY, "unexpected_position_policy": policy})
    assert halts.reasons  # always halts
    assert any(sev == "critical" for *_, sev in notes.sent)
    if policy == "protect":
        trades = await store.open_trades("paper")
        assert trades and trades[0].origin == "recovered"
        stops = [o for o in venue.state.orders.values() if o.type == "STOP_MARKET"]
        assert stops and stops[0].trigger_price == pytest.approx(84_000 * 0.985, rel=1e-3)
    elif policy == "flatten":
        assert venue.state.position.quantity == 0
    else:
        assert venue.state.position.quantity == pytest.approx(0.05) and not venue.open_ids()


async def test_orphan_orders_cancelled_foreign_untouched(store):
    venue, x, _, rec, *_ = rig(store)
    await venue.place_market_order(SYM, "BUY", 0.01, "tmp")
    await venue.place_stop_market(SYM, "SELL", 80_000, 0.01, x.cid(999, "SL", 1))
    await venue.place_stop_market(SYM, "SELL", 79_000, 0.01, "foreign-stop")
    await venue.place_market_order(SYM, "SELL", 0.01, "tmp2", reduce_only=True)  # flat again
    report = await rec.run(SAFETY)
    assert venue.open_ids() == ["foreign-stop"]
    assert report.get("foreign_orders") == 1


async def test_interrupted_close_resumes_monitoring(store):
    venue, x, _, rec, *_ = rig(store)
    res = await x.open_trade(plan())
    await store.update_trade(res.trade_id, status="closing")
    await rec.run(SAFETY)
    assert (await store.get_trade(res.trade_id)).status == "open"


async def test_back_to_back_trades_do_not_share_fills(store):
    """Regression: P&L must only include the trade's own fills, even when trades follow within seconds."""
    venue, x, mon, *_ = rig(store)
    r1 = await x.open_trade(plan("LONG"))
    venue.px.set(84_650)
    await venue.on_market()
    await mon.run_once(SAFETY)
    await mon.run_once(SAFETY)
    t1 = await store.get_trade(r1.trade_id)
    assert t1.status == "closed" and t1.realized_pnl > 0
    venue.px.set(84_650)
    r2 = await x.open_trade(plan("SHORT", entry=84_650))
    assert r2.ok, r2.message
    venue.px.set(84_860)
    await venue.on_market()
    await mon.run_once(SAFETY)
    t2 = await store.get_trade(r2.trade_id)
    assert t2.status == "closed" and t2.exit_reason == "stop_loss"
    own = {o.venue_order_id for o in await store.trade_orders(t2.id)}
    fills = [f for f in venue.state.fills if f.order_id in own]
    assert t2.gross_pnl == pytest.approx(sum(f.realized_pnl for f in fills))
    assert t2.fees == pytest.approx(sum(f.commission for f in fills))
    assert t2.realized_pnl < -20  # a full stop-out, not offset by the previous winner


async def test_liquidation_fill_counted_when_not_our_order(store):
    venue, x, mon, *_ = rig(store)
    res = await x.open_trade(plan())
    t = await store.get_trade(res.trade_id)
    venue.external_cancel(t.protection["sl"])  # no stop left → liquidation path
    venue.fail["place_stop_market"] = (99, ExchangeError("down"))
    liq = venue.liquidation_price()
    venue.px.set(liq - 50)
    await venue.on_market()
    await mon.run_once(SAFETY)
    t = await store.get_trade(t.id)
    assert t.status == "closed" and t.realized_pnl < -2000  # isolated margin lost


async def test_order_states_reflect_venue_after_close(store):
    venue, x, mon, *_ = rig(store)
    res = await x.open_trade(plan())
    venue.px.set(84_310)
    await venue.on_market()
    await mon.run_once(SAFETY)
    venue.px.set(84_620)
    await venue.on_market()
    await mon.run_once(SAFETY)
    by_kind = {}
    for o in await store.trade_orders(res.trade_id):
        by_kind.setdefault(o.kind, []).append(o.status)
    assert by_kind["entry"] == ["FILLED"]
    assert by_kind["tp1"] == ["FILLED"] and by_kind["tp2"] == ["FILLED"]
    assert set(by_kind["sl"]) == {"CANCELED"}
