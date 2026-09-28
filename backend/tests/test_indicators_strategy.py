from __future__ import annotations

import numpy as np

from app.core import params as P
from app.market import indicators as ind
from app.market.features import TFView, compute_features
from app.strategy.base import StrategyState
from app.strategy.breakout_retest import BreakoutRetestStrategy
from tests.helpers import breakout_retest_5m, extras, mirror, trend_series, views_for

S = BreakoutRetestStrategy()


# ----------------------------------------------------------------------------- indicators
def test_ema_seed_and_recursion():
    x = np.arange(1, 21, dtype=float)
    e = ind.ema(x, 5)
    assert np.isnan(e[3])
    assert e[4] == 3.0  # SMA seed of 1..5
    alpha = 2 / 6
    assert abs(e[5] - (alpha * 6 + (1 - alpha) * 3.0)) < 1e-12


def test_rsi_extremes_and_range():
    up = np.arange(100, 140, dtype=float)
    assert ind.rsi(up, 14)[-1] == 100.0
    down = up[::-1].copy()
    assert ind.rsi(down, 14)[-1] < 1e-9
    flat = np.full(40, 5.0)
    assert ind.rsi(flat, 14)[-1] == 50.0


def test_atr_constant_range():
    n = 50
    close = np.full(n, 100.0)
    a = ind.atr(close + 2, close - 2, close, 14)
    assert abs(a[-1] - 4.0) < 1e-9


def test_vwap_resets_daily():
    t = np.array([0, 300_000, 86_400_000, 86_700_000], dtype=np.int64)
    h = np.array([11, 11, 21, 21.0])
    lo = np.array([9, 9, 19, 19.0])
    c = np.array([10, 10, 20, 20.0])
    v = np.array([1, 1, 1, 1.0])
    vw = ind.session_vwap(t, h, lo, c, v)
    assert vw[1] == 10 and vw[2] == 20  # new UTC day resets


def test_pivots_are_confirmed_only_after_strength_bars():
    high = np.array([1, 2, 3, 9, 3, 2, 1, 1, 1], dtype=float)
    low = high - 0.5
    pv = [p for p in ind.pivots(high, low, 3) if p.kind == "high"]
    assert len(pv) == 1 and pv[0].index == 3 and pv[0].confirmed_at == 6


def test_cluster_levels_counts_touches():
    pv = [ind.Pivot(1, 4, 100.0, "high"), ind.Pivot(5, 8, 100.2, "high"), ind.Pivot(9, 12, 105.0, "high")]
    lv = ind.cluster_levels(pv, "resistance", tolerance=0.5)
    assert [x.touches for x in lv] == [2, 1]
    assert lv[1].extreme


def test_trend_score_labels():
    assert ind.trend_score(110, 108, 105, 100, 0.01, 2) == 2
    assert ind.trend_score(90, 92, 95, 100, -0.01, 2) == -2
    assert ind.trend_score(101, 99, 100.5, 100, 0.0, 2) == 1
    assert ind.trend_score(float("nan"), 1, 1, 1, 1, 1) == 0


# ----------------------------------------------------------------------------- strategy
def test_long_breakout_retest_setup():
    ev = S.evaluate(views_for(breakout_retest_5m()), extras(), P.defaults())
    assert ev.decision == "LONG"
    s = ev.setup
    assert s.stop < s.level < s.entry < s.tp1 < s.tp2
    assert s.rr >= P.defaults()["min_rr"]
    assert any("breakout" in r for r in s.reasons)
    assert any("retest" in r for r in s.reasons)
    assert all(c.passed for c in ev.long_checks)


def test_short_is_mirror_of_long():
    ev = S.evaluate(views_for(mirror(breakout_retest_5m()), htf_dir=-1), extras(funding=-0.0001), P.defaults())
    assert ev.decision == "SHORT"
    s = ev.setup
    assert s.stop > s.level > s.entry > s.tp1 > s.tp2


def test_no_trade_without_volume_confirmation():
    ev = S.evaluate(views_for(breakout_retest_5m(volume_mult=1.0)), extras(), P.defaults())
    assert ev.decision == "NO_TRADE"
    assert any(c.name == "breakout volume" and not c.passed for c in ev.long_checks)
    assert ev.summary.startswith("NO TRADE")


def test_no_trade_when_retest_fails():
    ev = S.evaluate(views_for(breakout_retest_5m(fail_retest=True)), extras(), P.defaults())
    assert ev.decision == "NO_TRADE"


def test_no_trade_while_waiting_for_retest():
    ev = S.evaluate(views_for(breakout_retest_5m(bars_after=0)), extras(), P.defaults())
    assert ev.decision == "NO_TRADE"
    assert any(c.name == "retest" and not c.passed for c in ev.long_checks)


def test_strong_bearish_htf_blocks_long():
    s5 = breakout_retest_5m()
    end = float(s5.close[-1])
    v = views_for(s5, s1h=trend_series("1h", end, 250, 60, -1), s4h=trend_series("4h", end, 250, 150, -1))
    ev = S.evaluate(v, extras(), P.defaults())
    assert ev.decision == "NO_TRADE"
    assert not next(c for c in ev.long_checks if c.name == "1h trend").passed


def test_crowded_funding_lowers_confidence():
    base = S.evaluate(views_for(breakout_retest_5m()), extras(funding=0.0001), P.defaults()).setup.confidence
    crowded = S.evaluate(views_for(breakout_retest_5m()), extras(funding=0.0008), P.defaults())
    assert crowded.setup is None or crowded.setup.confidence < base


def test_signal_cooldown_blocks_repeat():
    st = StrategyState()
    v = views_for(breakout_retest_5m())
    ev = S.evaluate(v, extras(), P.defaults(), st)
    st.last_signal_bar["LONG"] = ev.bar_time
    ev2 = S.evaluate(v, extras(), P.defaults(), st)
    assert ev2.decision == "NO_TRADE"


def test_min_confidence_parameter_respected():
    p = P.defaults()
    p["min_confidence"] = 100
    ev = S.evaluate(views_for(breakout_retest_5m()), extras(oi_change=None, funding=0.0002), p)
    assert ev.decision == "NO_TRADE"


def test_no_lookahead_evaluation_before_breakout():
    """Evaluating at a bar before the breakout must not see the breakout."""
    s5 = breakout_retest_5m()
    p = P.defaults()
    full = compute_features(s5, p)
    v = views_for(s5)
    b = len(s5) - 4  # breakout bar index
    v["5m"] = TFView(s5, full, b - 1)
    ev = S.evaluate(v, extras(), p)
    assert ev.decision == "NO_TRADE"
    # features computed on the truncated series are identical at that index (causality)
    trunc = compute_features(s5.upto(b), p)
    for name in ("ema_fast", "ema_mid", "ema_slow", "rsi", "atr"):
        assert abs(getattr(trunc, name)[b - 1] - getattr(full, name)[b - 1]) < 1e-9


def test_evaluation_is_deterministic():
    v = views_for(breakout_retest_5m())
    a = S.evaluate(v, extras(), P.defaults()).to_dict()
    b = S.evaluate(v, extras(), P.defaults()).to_dict()
    assert a == b


# ----------------------------------------------------------------------------- params
def test_defaults_are_valid_and_conservative():
    clean, errors = P.validate(P.defaults())
    assert not errors
    assert clean["risk_per_trade_pct"] == 0.5
    assert clean["max_daily_loss_pct"] == 2.0
    assert clean["max_weekly_loss_pct"] == 5.0
    assert clean["max_leverage"] == 3
    assert clean["max_open_positions"] == 1
    assert clean["min_rr"] == 2.0
    assert clean["ai_mode"] == "required"


def test_hard_caps_cannot_be_exceeded():
    clean, errors = P.validate({"risk_per_trade_pct": 5, "max_leverage": 25, "max_daily_loss_pct": 50})
    assert len(errors) == 3
    assert clean["risk_per_trade_pct"] == 0.5 and clean["max_leverage"] == 3


def test_locked_params_ignore_edits():
    clean, errors = P.validate({"require_stop_loss": False, "max_open_positions": 5})
    assert clean["require_stop_loss"] is True and clean["max_open_positions"] == 1
    assert not errors


def test_cross_field_validation():
    _, errors = P.validate({"ema_fast": 50, "ema_mid": 25})
    assert any("fast < mid < slow" in e for e in errors)
    _, errors = P.validate({"tp1_r": 4, "tp2_r": 3})
    assert any("TP1" in e for e in errors)


def test_unknown_and_malformed_params_rejected():
    clean, errors = P.validate({"bogus": 1, "risk_per_trade_pct": "abc", "breakeven_after_tp1": "maybe"})
    assert len(errors) == 3
    assert clean["risk_per_trade_pct"] == 0.5
