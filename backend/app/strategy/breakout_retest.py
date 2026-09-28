"""Breakout-retest intraday strategy (v1) for BTCUSDT perpetual.

LONG (SHORT is the mirror image):
 1. Higher-timeframe context is not against the trade (1h / 4h trend scores).
 2. 15m trend neutral or aligned (optional).
 3. 5m momentum: fast EMA above mid EMA, RSI inside the long band.
 4. A *meaningful* resistance (clustered 5m swing highs: ≥N touches, or the
    extreme of the look-back) was broken by a bullish candle closing clearly
    above it on above-average volume, within the last K bars.
 5. Price pulled back to the level (retest), wicks did not pierce too deep and
    every close since the breakout held above the level.
 6. The latest closed candle confirms (bullish, above the fast EMA) without
    having run too far from the level.
 7. Stop goes beyond the retest extreme (ATR-buffered, bounded by min/max ATR);
    TP1/TP2 in R multiples, TP2 capped at the next opposing 1h level.
 8. Deterministic confidence score; the setup must clear the minimum.

Every evaluation — including NO_TRADE — carries the full checklist so the UI can
show exactly why nothing happened.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from app.core.params import STRATEGY_NAME
from app.market import indicators as ind
from app.market.features import TFView
from app.strategy.base import Check, Evaluation, MarketExtras, Setup, StrategyState


def _nan(x: float) -> bool:
    return x is None or (isinstance(x, float) and math.isnan(x))


def _fmt(x: float) -> str:
    return f"{x:,.1f}"


def classify_regime(v5: TFView, v1h: TFView, v4h: TFView, p: dict[str, Any]) -> str:
    atr_pct = v5.atr / v5.close * 100 if v5.close else float("nan")
    if _nan(atr_pct):
        return "unknown"
    if atr_pct > p["max_atr_pct"] * 0.8:
        return "high_volatility"
    if atr_pct < max(p["min_atr_pct"] * 1.5, 0.03):
        return "quiet"
    t1, t4 = v1h.trend(), v4h.trend()
    if t1 >= 1 and t4 >= 0:
        return "trending_up"
    if t1 <= -1 and t4 <= 0:
        return "trending_down"
    return "ranging"


class BreakoutRetestStrategy:
    name = STRATEGY_NAME

    def evaluate(self, views: dict[str, TFView], extras: MarketExtras, p: dict[str, Any],
                 state: StrategyState | None = None) -> Evaluation:
        v5 = views["5m"]
        state = state or StrategyState()
        bar_time = int(v5.series.open_time[v5.idx])
        price = v5.close
        regime = classify_regime(v5, views["1h"], views["4h"], p)
        tfs = {k: v.summary() for k, v in views.items() if v.idx >= 0}

        long_setup, long_checks = self._direction(1, views, extras, p, state, bar_time)
        short_setup, short_checks = self._direction(-1, views, extras, p, state, bar_time)
        candidates = [s for s in (long_setup, short_setup) if s]
        setup = max(candidates, key=lambda s: s.confidence) if candidates else None
        decision = setup.direction if setup else "NO_TRADE"
        levels = self._nearby_levels(v5, p)

        if setup:
            summary = f"{setup.direction} setup · confidence {setup.confidence:.0f} · R:R {setup.rr:.2f}"
        else:
            summary = "NO TRADE — " + "; ".join(
                self._first_failures(long_checks, "long") + self._first_failures(short_checks, "short"))
        return Evaluation(bar_time=bar_time, price=price, decision=decision, setup=setup, long_checks=long_checks,
                          short_checks=short_checks, summary=summary, regime=regime, timeframes=tfs, levels=levels,
                          strategy=self.name)

    @staticmethod
    def _first_failures(checks: list[Check], label: str) -> list[str]:
        failed = [c for c in checks if not c.passed]
        return [f"{label}: {failed[0].detail or failed[0].name}"] if failed else []

    # ------------------------------------------------------------------ core
    def _direction(self, d: int, views: dict[str, TFView], ex: MarketExtras, p: dict[str, Any],
                   state: StrategyState, bar_time: int) -> tuple[Setup | None, list[Check]]:
        side = "LONG" if d == 1 else "SHORT"
        v5, v15, v1h, v4h = views["5m"], views["15m"], views["1h"], views["4h"]
        checks: list[Check] = []
        reasons: list[str] = []
        warnings: list[str] = []

        def check(name: str, ok: bool, detail: str) -> bool:
            checks.append(Check(name, bool(ok), detail))
            return bool(ok)

        i = v5.idx
        s = v5.series
        f = v5.feat
        atr5 = v5.atr
        if i < max(p["ema_slow"], p["level_lookback_bars"] // 2) or _nan(atr5) or atr5 <= 0:
            check("data", False, "not enough 5m history")
            return None, checks

        # 1-2. Higher-timeframe context --------------------------------------------------
        t15, t1, t4 = v15.trend(), v1h.trend(), v4h.trend()
        if d == 1:
            ok1 = t1 >= p["long_min_1h_trend"]
            ok4 = t4 >= p["long_min_4h_trend"]
        else:
            ok1 = t1 <= p["short_max_1h_trend"]
            ok4 = t4 <= p["short_max_4h_trend"]
        check("1h trend", ok1, f"1h {ind.TREND_LABELS[t1]}")
        check("4h trend", ok4, f"4h {ind.TREND_LABELS[t4]}")
        ok15 = (d * t15 >= 0) if p["require_15m_alignment"] else True
        check("15m trend", ok15, f"15m {ind.TREND_LABELS[t15]}")
        if d * t1 >= 1:
            reasons.append(f"1h trend {ind.TREND_LABELS[t1]}")
        if d * t15 >= 1:
            reasons.append(f"15m trend {ind.TREND_LABELS[t15]}")
        if d * t4 >= 1:
            reasons.append(f"4h trend {ind.TREND_LABELS[t4]}")

        # Volatility
        atr_pct = atr5 / v5.close * 100
        check("volatility", p["min_atr_pct"] <= atr_pct <= p["max_atr_pct"],
              f"5m ATR {atr_pct:.3f}% (allowed {p['min_atr_pct']}–{p['max_atr_pct']}%)")

        # 3. Momentum --------------------------------------------------------------------
        ef, em = v5.v(f.ema_fast), v5.v(f.ema_mid)
        r = v5.v(f.rsi)
        check("5m momentum", d * (ef - em) > 0,
              f"EMA{p['ema_fast']} {'above' if ef > em else 'below'} EMA{p['ema_mid']}")
        if d == 1:
            rsi_ok = p["rsi_long_min"] <= r <= p["rsi_long_max"]
            band = f"{p['rsi_long_min']:.0f}–{p['rsi_long_max']:.0f}"
        else:
            rsi_ok = p["rsi_short_min"] <= r <= p["rsi_short_max"]
            band = f"{p['rsi_short_min']:.0f}–{p['rsi_short_max']:.0f}"
        check("RSI", rsi_ok, f"RSI {r:.1f} (band {band})")

        # 4. Breakout of a meaningful level ---------------------------------------------
        kind = "resistance" if d == 1 else "support"
        pv_kind = "high" if d == 1 else "low"
        lookback = int(p["level_lookback_bars"])
        max_age = int(p["breakout_max_age_bars"])
        found: tuple[int, ind.Level] | None = None
        for b in range(i, max(i - max_age, 1), -1):
            atr_b = v5.v(f.atr, i - b)
            if _nan(atr_b) or atr_b <= 0:
                continue
            pv = [x for x in v5.pivots_between(b - lookback, b - 1, b - 1) if x.kind == pv_kind]
            levels = ind.cluster_levels(pv, kind, p["level_cluster_atr"] * atr_b)
            crossed = [lv for lv in levels
                       if (lv.touches >= p["min_level_touches"] or lv.extreme)
                       and d * (s.close[b] - lv.price) > 0 and d * (s.close[b - 1] - lv.price) <= 0]
            if crossed:
                lv = max(crossed, key=lambda x: d * x.price)
                found = (b, lv)
                break
        if not found:
            check("breakout", False, f"no {kind} break in the last {max_age} bars")
            return None, checks
        b, level = found
        L = level.price
        atr_b = float(f.atr[b])
        margin_atr = d * (s.close[b] - L) / atr_b
        check("breakout", margin_atr >= p["breakout_buffer_atr"] and d * (s.close[b] - s.open[b]) > 0,
              f"{'broke' if d == 1 else 'broke below'} {kind} {_fmt(L)} ({level.touches} touch"
              f"{'es' if level.touches != 1 else ''}) by {margin_atr:.2f} ATR, {i - b} bars ago")
        vol_base = float(f.vol_sma[b - 1]) if b >= 1 and not _nan(float(f.vol_sma[b - 1])) else float("nan")
        vr = float(s.volume[b]) / vol_base if vol_base and not _nan(vol_base) else 0.0
        check("breakout volume", vr >= p["volume_threshold"],
              f"breakout volume {vr:.2f}× average (need {p['volume_threshold']:.2f}×)")
        reasons.append(f"5m {kind} {'breakout' if d == 1 else 'breakdown'} at {_fmt(L)}"
                       f" ({level.touches} touches)" if level.touches > 1 else
                       f"5m {kind} {'breakout' if d == 1 else 'breakdown'} at {_fmt(L)} (range extreme)")
        if vr > 0:
            reasons.append(f"volume {(vr - 1) * 100:+.0f}% vs average")

        # 5. Retest --------------------------------------------------------------------
        if i <= b:
            check("retest", False, "breakout just happened — waiting for a retest")
            return None, checks
        seg_low = s.low[b + 1:i + 1]
        seg_high = s.high[b + 1:i + 1]
        seg_close = s.close[b + 1:i + 1]
        extreme = float(seg_low.min()) if d == 1 else float(seg_high.max())
        touched = d * (extreme - L) <= p["retest_tolerance_atr"] * atr5
        not_deep = d * (L - extreme) <= p["retest_max_penetration_atr"] * atr5
        holds = bool(np.all(d * (seg_close - L) >= 0))
        check("retest", touched, f"pullback reached {_fmt(extreme)} ({d * (extreme - L) / atr5:+.2f} ATR from level)")
        check("retest held", not_deep and holds,
              "closes held the level" if holds else "a candle closed back through the level (failed breakout)")
        if touched and not_deep and holds:
            reasons.append("successful retest" if d == 1 else "retest rejected at broken support")

        # 6. Confirmation + extension ---------------------------------------------------
        confirm = d * (s.close[i] - s.open[i]) > 0 and d * (s.close[i] - ef) > 0
        check("confirmation", confirm,
              f"last candle {'bullish' if s.close[i] > s.open[i] else 'bearish'}, close "
              f"{'above' if s.close[i] > ef else 'below'} EMA{p['ema_fast']}")
        ext = d * (s.close[i] - L) / atr5
        check("not extended", ext <= p["max_entry_extension_atr"],
              f"price {ext:.2f} ATR from level (max {p['max_entry_extension_atr']})")

        # 7. Stop and targets ---------------------------------------------------------
        entry = float(s.close[i])
        stop_ref = min(extreme, float(s.low[i])) if d == 1 else max(extreme, float(s.high[i]))
        stop = stop_ref - d * p["stop_buffer_atr"] * atr5
        risk = d * (entry - stop)
        if risk < p["min_stop_atr"] * atr5:
            stop = entry - d * p["min_stop_atr"] * atr5
            risk = d * (entry - stop)
        stop_ok = risk <= p["max_stop_atr"] * atr5 and risk > 0
        check("stop distance", stop_ok, f"stop {_fmt(stop)} = {risk / atr5:.2f} ATR ({risk / entry * 100:.2f}%)")
        if not stop_ok:
            return None, checks
        tp1 = entry + d * p["tp1_r"] * risk
        tp2 = entry + d * p["tp2_r"] * risk
        opp = self._opposing_htf_level(d, v1h, entry, risk, p)
        if opp is not None and d * (opp - tp2) < 0:
            tp2_capped = opp - d * 0.1 * atr5
            warnings.append(f"TP2 capped below 1h {'resistance' if d == 1 else 'support'} {_fmt(opp)}")
            tp2 = tp2_capped
        if d * (tp2 - tp1) <= 0:
            check("room to target", False, f"opposing 1h level {_fmt(opp or 0)} blocks the move before TP1")
            return None, checks
        check("room to target", True, "no opposing 1h level before TP1")
        frac = float(p["tp1_close_fraction"])
        rr1 = d * (tp1 - entry) / risk
        rr2 = d * (tp2 - entry) / risk
        rr = frac * rr1 + (1 - frac) * rr2
        check("reward:risk", rr >= p["min_rr"], f"blended R:R {rr:.2f} (TP1 {rr1:.1f}R, TP2 {rr2:.1f}R)")
        reasons.append(f"RSI {r:.0f}")
        reasons.append(f"R:R {rr:.1f}")

        # Derivatives context (confidence only, hard limits are in the risk engine) ------
        conf = 50.0
        conf += 10 if d * t1 >= 1 else 0
        conf += 5 if d * t4 >= 1 else 0
        conf += 5 if d * t15 >= 1 else 0
        conf += min(15.0, max(0.0, (vr - 1.0) * 15))
        mid_band = (55 <= r <= 65) if d == 1 else (35 <= r <= 45)
        conf += 5 if mid_band else 0
        vwap = v5.v(f.vwap)
        if not _nan(vwap) and d * (entry - vwap) > 0:
            conf += 5
            reasons.append(f"price {'above' if d == 1 else 'below'} VWAP")
        conf += 5 if level.touches >= 3 else 0
        conf += 5 if rr >= 2.5 else 0
        conf -= 5 if ext > 0.6 else 0
        fr = ex.funding_rate
        if fr is not None:
            crowded = p["crowded_funding_pct"] / 100
            if d * fr > crowded:
                conf -= 10
                warnings.append(f"funding {fr * 100:.3f}% — {'longs' if d == 1 else 'shorts'} crowded")
            else:
                reasons.append(f"funding {'neutral' if abs(fr) < crowded / 2 else 'acceptable'} ({fr * 100:.4f}%)")
        if ex.oi_change_1h_pct is not None:
            if ex.oi_change_1h_pct > 0.5:
                conf += 5
                reasons.append(f"open interest +{ex.oi_change_1h_pct:.1f}% (1h)")
            elif ex.oi_change_1h_pct < -1.5:
                conf -= 5
                warnings.append(f"open interest {ex.oi_change_1h_pct:.1f}% (1h) — move driven by closing positions")
        conf = max(0.0, min(100.0, conf))
        check("confidence", conf >= p["min_confidence"], f"confidence {conf:.0f} (min {p['min_confidence']:.0f})")

        # Cooldown
        last = state.last_signal_bar.get(side)
        cool_ms = int(p["signal_cooldown_bars"]) * 300_000
        cooled = last is None or bar_time - last > cool_ms
        check("signal cooldown", cooled, "cooldown clear" if cooled else "same-direction signal fired recently")

        if not all(c.passed for c in checks):
            return None, checks
        return Setup(direction=side, entry=entry, stop=stop, tp1=tp1, tp2=tp2, rr=rr, rr_tp2=rr2, confidence=conf,
                     level=L, reasons=reasons, warnings=warnings, breakout_bar=int(s.open_time[b]),
                     tp1_fraction=frac), checks

    @staticmethod
    def _opposing_htf_level(d: int, v1h: TFView, entry: float, risk: float, p: dict[str, Any]) -> float | None:
        if v1h.idx < 10 or _nan(v1h.atr):
            return None
        pv = [x for x in v1h.pivots_between(max(0, v1h.idx - 200), v1h.idx, v1h.idx)
              if x.kind == ("high" if d == 1 else "low")]
        lv = ind.cluster_levels(pv, "resistance" if d == 1 else "support", 0.3 * v1h.atr)
        ahead = [x.price for x in lv if d * (x.price - entry) > 0.25 * risk]
        if not ahead:
            return None
        return min(ahead) if d == 1 else max(ahead)

    @staticmethod
    def _nearby_levels(v5: TFView, p: dict[str, Any]) -> list[dict[str, Any]]:
        i = v5.idx
        if i < 10 or _nan(v5.atr):
            return []
        pv = v5.pivots_between(max(0, i - int(p["level_lookback_bars"])), i, i)
        tol = p["level_cluster_atr"] * v5.atr
        out = []
        for kind in ("resistance", "support"):
            for lv in ind.cluster_levels(pv, kind, tol):
                if lv.touches >= p["min_level_touches"] or lv.extreme:
                    out.append(lv.to_dict())
        price = v5.close
        out.sort(key=lambda x: abs(x["price"] - price))
        return out[:8]
