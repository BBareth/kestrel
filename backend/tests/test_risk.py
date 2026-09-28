from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.core import params as P
from app.exchange.models import DEFAULT_BTCUSDT_FILTERS as F
from app.risk.engine import AccountState, RiskEngine, SystemGates, TradeStats, isolated_liquidation_price
from app.strategy.base import Setup

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)  # Monday midday, far from funding


def setup(direction="LONG", entry=84_000.0, stop=83_800.0, rr=2.25):  # noqa: ANN001
    d = 1 if direction == "LONG" else -1
    risk = abs(entry - stop)
    return Setup(direction=direction, entry=entry, stop=stop, tp1=entry + d * 1.5 * risk, tp2=entry + d * 3 * risk,
                 rr=rr, rr_tp2=3.0, confidence=80, level=entry - d * 50)


def stats(**kw):  # noqa: ANN001, ANN201
    base = dict(day_start_equity=10_000, week_start_equity=10_000)
    base.update(kw)
    return TradeStats(**base)


def run(s=None, acct=None, st=None, gates=None, funding=0.0001, spread=0.5, params=None, now=NOW):  # noqa: ANN001, ANN201
    p = params or P.defaults()
    return RiskEngine(p).evaluate(s or setup(), acct or AccountState(10_000, 10_000, 10_000), st or stats(),
                                  gates or SystemGates(), F, funding, spread, now, None)


def test_approves_clean_setup_and_sizes_from_risk():
    d = run(setup(stop=83_600))
    assert d.approved, d.blocked_by
    sz = d.sizing
    p = P.defaults()
    per_unit = 400 + 84_000 * (2 * p["taker_fee_pct"] / 100 + p["paper_stop_slippage_bps"] / 10_000)
    expected = int((10_000 * 0.005 / per_unit) * 1000) / 1000  # floor to 0.001
    assert sz.quantity == pytest.approx(expected)
    assert sz.risk_usdt <= 50.0 + 1e-9  # never above 0.5% of equity
    assert sz.leverage == 3
    assert sz.margin == pytest.approx(sz.notional / 3)


def test_sizing_depends_on_stop_distance_not_fixed_amount():
    wide = run(setup(stop=83_300)).sizing
    tight = run(setup(stop=83_600)).sizing
    assert tight.quantity > wide.quantity
    assert abs(tight.risk_usdt - wide.risk_usdt) < 5  # same risk budget


def test_tp_split_and_single_tp_fallback():
    d = run()
    assert d.sizing.split and abs(d.sizing.tp1_qty + d.sizing.tp2_qty - d.sizing.quantity) < 1e-9
    small = run(acct=AccountState(700, 700, 700), s=setup(stop=83_200)).sizing
    if small is not None:
        assert small.split is False or small.tp1_qty >= 0.001


def test_rejects_account_too_small():
    d = run(acct=AccountState(50, 50, 50))
    assert not d.approved
    assert any("minimum" in b for b in d.blocked_by)


def test_notional_cap_and_margin_cap():
    p = P.defaults()
    p["max_position_notional_usdt"] = 1_000
    d = run(params=p, s=setup(stop=83_990))
    assert d.sizing.notional <= 1_000 + 1e-6
    d2 = run(acct=AccountState(10_000, 400, 10_000), s=setup(stop=83_990))
    assert d2.sizing.margin <= 400 * 0.5 + 1e-6


@pytest.mark.parametrize("gate,field", [
    (SystemGates(kill_switch=True), "kill switch"),
    (SystemGates(strategy_enabled=False), "strategy enabled"),
    (SystemGates(halted=True, halt_reason="x"), "halt"),
    (SystemGates(guard_reasons=["market data stale"]), "safety guards"),
])
def test_system_gates_block(gate, field):  # noqa: ANN001
    d = run(gates=gate)
    assert not d.approved
    assert any(c.name == field and not c.passed for c in d.checks)


def test_daily_loss_limit_blocks():
    d = run(st=stats(realized_today=-200))  # 2% of 10k
    assert not d.approved and any("today" in b for b in d.blocked_by)
    assert run(st=stats(realized_today=-199)).approved


def test_weekly_loss_limit_blocks():
    d = run(st=stats(realized_week=-500))
    assert not d.approved


def test_losing_streak_cooldown():
    st = stats(consecutive_losses=3, last_loss_at=NOW - timedelta(minutes=100))
    assert not run(st=st).approved
    st2 = stats(consecutive_losses=3, last_loss_at=NOW - timedelta(minutes=241))
    assert run(st=st2).approved


def test_cooldown_after_single_loss():
    assert not run(st=stats(consecutive_losses=1, last_loss_at=NOW - timedelta(minutes=10))).approved
    assert run(st=stats(consecutive_losses=1, last_loss_at=NOW - timedelta(minutes=31))).approved


def test_frequency_limits():
    assert not run(st=stats(trades_last_hour=2)).approved
    assert not run(st=stats(trades_today=6)).approved


def test_duplicate_and_max_positions():
    assert not run(st=stats(open_positions=1)).approved
    assert not run(st=stats(has_open_trade=True)).approved


def test_funding_spread_and_unknowns():
    assert not run(funding=0.001).approved  # 0.1% > 0.05%
    assert not run(spread=10).approved
    assert not run(funding=None).approved  # unknown ⇒ no trade
    assert not run(spread=None).approved


def test_min_rr_and_invalid_stop():
    assert not run(setup(rr=1.5)).approved
    bad = setup()
    bad.stop = 84_100  # wrong side
    d = run(bad)
    assert not d.approved and d.sizing is None


def test_trading_window_and_weekend():
    p = P.defaults()
    p["trading_start_utc"], p["trading_end_utc"] = "13:00", "20:00"
    assert not run(params=p).approved
    p = P.defaults()
    p["weekend_trading"] = False
    saturday = datetime(2026, 10, 3, 12, 0, tzinfo=UTC)
    assert not run(params=p, now=saturday).approved


def test_funding_window_guard():
    p = P.defaults()
    near = datetime(2026, 9, 28, 7, 55, tzinfo=UTC)
    next_f = int(datetime(2026, 9, 28, 8, 0, tzinfo=UTC).timestamp() * 1000)
    d = RiskEngine(p).evaluate(setup(), AccountState(10_000, 10_000, 10_000), stats(), SystemGates(), F, 0.0001, 0.5,
                               near, next_f)
    assert not d.approved and any("funding timestamp" in b for b in d.blocked_by)


def test_hard_caps_hold_even_if_params_tampered():
    p = P.defaults()
    p["risk_per_trade_pct"] = 50  # bypassing validation on purpose
    p["max_leverage"] = 100
    d = RiskEngine(p).evaluate(setup(), AccountState(10_000, 10_000, 10_000), stats(), SystemGates(), F, 0.0001, 0.5,
                               NOW, None)
    assert d.sizing.risk_pct <= 2.0 + 1e-9
    assert d.sizing.leverage <= 10


def test_liquidation_price_formula_and_buffer():
    liq = isolated_liquidation_price("LONG", 100_000, 3)
    assert 66_000 < liq < 67_200
    liq_s = isolated_liquidation_price("SHORT", 100_000, 3)
    assert 132_000 < liq_s < 133_400
    p = P.defaults()
    p["min_liq_distance_multiple"] = 20
    d = run(params=p, s=setup(stop=82_000))
    assert not d.approved


def test_short_side_sizing():
    d = run(setup("SHORT", entry=84_000, stop=84_200))
    assert d.approved and d.sizing.quantity > 0
