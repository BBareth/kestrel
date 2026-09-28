"""Technical indicators (numpy, causal).

Every output at index ``i`` depends only on inputs at indices ``<= i`` — with the
single documented exception of swing pivots, which are only *known* ``strength``
bars after they form; ``pivots`` returns the confirmation index so callers can
never use a swing before it would have been visible.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

NAN = float("nan")


def sma(x: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) < period or period <= 0:
        return out
    c = np.cumsum(np.insert(x.astype(float), 0, 0.0))
    out[period - 1:] = (c[period:] - c[:-period]) / period
    return out


def ema(x: np.ndarray, period: int) -> np.ndarray:
    """EMA seeded with the SMA of the first ``period`` values."""
    n = len(x)
    out = np.full(n, np.nan)
    if n < period or period <= 0:
        return out
    alpha = 2.0 / (period + 1)
    out[period - 1] = float(np.mean(x[:period]))
    for i in range(period, n):
        out[i] = alpha * x[i] + (1 - alpha) * out[i - 1]
    return out


def rma(x: np.ndarray, period: int) -> np.ndarray:
    """Wilder's moving average (used by RSI and ATR)."""
    n = len(x)
    out = np.full(n, np.nan)
    if n < period or period <= 0:
        return out
    out[period - 1] = float(np.mean(x[:period]))
    for i in range(period, n):
        out[i] = (out[i - 1] * (period - 1) + x[i]) / period
    return out


def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    n = len(close)
    out = np.full(n, np.nan)
    if n <= period:
        return out
    delta = np.diff(close.astype(float))
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    ag = rma(gain, period)
    al = rma(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = ag / al
        r = 100.0 - 100.0 / (1.0 + rs)
    r = np.where((al == 0) & (ag > 0), 100.0, r)
    r = np.where((al == 0) & (ag == 0), 50.0, r)
    out[1:] = r
    return out


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    prev_close = np.concatenate(([close[0]], close[:-1]))
    return np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])


def atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    return rma(true_range(high, low, close), period)


def session_vwap(open_time_ms: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray,
                 volume: np.ndarray) -> np.ndarray:
    """VWAP reset at each UTC day boundary."""
    tp = (high + low + close) / 3.0
    day = open_time_ms // 86_400_000
    out = np.full(len(close), np.nan)
    cum_pv = 0.0
    cum_v = 0.0
    last_day = None
    for i in range(len(close)):
        if day[i] != last_day:
            cum_pv = 0.0
            cum_v = 0.0
            last_day = day[i]
        cum_pv += tp[i] * volume[i]
        cum_v += volume[i]
        out[i] = cum_pv / cum_v if cum_v > 0 else tp[i]
    return out


def slope(x: np.ndarray, lookback: int) -> np.ndarray:
    """Relative change of ``x`` over ``lookback`` bars (fraction)."""
    out = np.full(len(x), np.nan)
    if len(x) > lookback:
        with np.errstate(divide="ignore", invalid="ignore"):
            out[lookback:] = (x[lookback:] - x[:-lookback]) / x[:-lookback]
    return out


@dataclass(frozen=True)
class Pivot:
    index: int  # bar where the swing extreme occurred
    confirmed_at: int  # first bar at which the swing is known (index + strength)
    price: float
    kind: str  # "high" | "low"


def pivots(high: np.ndarray, low: np.ndarray, strength: int) -> list[Pivot]:
    """Swing highs/lows that dominate ``strength`` bars on each side."""
    out: list[Pivot] = []
    n = len(high)
    for i in range(strength, n - strength):
        h = high[i]
        window_h = high[i - strength:i + strength + 1]
        if h == window_h.max() and np.sum(window_h == h) == 1:
            out.append(Pivot(i, i + strength, float(h), "high"))
        lo = low[i]
        window_l = low[i - strength:i + strength + 1]
        if lo == window_l.min() and np.sum(window_l == lo) == 1:
            out.append(Pivot(i, i + strength, float(lo), "low"))
    return out


@dataclass
class Level:
    price: float
    kind: str  # "resistance" | "support"
    touches: int
    first_index: int
    last_index: int
    last_confirmed_at: int
    extreme: bool = False  # the highest high / lowest low of the look-back

    def to_dict(self) -> dict:
        return {"price": round(self.price, 2), "kind": self.kind, "touches": self.touches, "extreme": self.extreme}


def cluster_levels(pvts: list[Pivot], kind: str, tolerance: float) -> list[Level]:
    """Merge swing prices closer than ``tolerance`` into levels (touch counts)."""
    src = sorted((p for p in pvts if p.kind == ("high" if kind == "resistance" else "low")), key=lambda p: p.price)
    levels: list[Level] = []
    for p in src:
        if levels and abs(p.price - levels[-1].price) <= tolerance:
            lv = levels[-1]
            lv.price = (lv.price * lv.touches + p.price) / (lv.touches + 1)
            lv.touches += 1
            lv.first_index = min(lv.first_index, p.index)
            lv.last_index = max(lv.last_index, p.index)
            lv.last_confirmed_at = max(lv.last_confirmed_at, p.confirmed_at)
        else:
            levels.append(Level(p.price, kind, 1, p.index, p.index, p.confirmed_at))
    if levels:
        ext = max(levels, key=lambda lv: lv.price) if kind == "resistance" else min(levels, key=lambda lv: lv.price)
        ext.extreme = True
    return levels


def trend_score(close: float, ema_fast: float, ema_mid: float, ema_slow: float, mid_slope: float,
                atr_value: float) -> int:
    """Trend classification on one timeframe: −2 strong bear … +2 strong bull.

    +2: price > fast > mid > slow and mid rising, with price clearly above slow (> 0.5 ATR)
    +1: price above slow and mid above slow (bullish structure)
     0: mixed / flat
    −1 / −2: mirror images.
    """
    vals = (close, ema_fast, ema_mid, ema_slow, mid_slope, atr_value)
    if any(v is None or np.isnan(v) for v in vals):
        return 0
    if close > ema_fast > ema_mid > ema_slow and mid_slope > 0 and (close - ema_slow) > 0.5 * atr_value:
        return 2
    if close < ema_fast < ema_mid < ema_slow and mid_slope < 0 and (ema_slow - close) > 0.5 * atr_value:
        return -2
    if close > ema_slow and ema_mid > ema_slow:
        return 1
    if close < ema_slow and ema_mid < ema_slow:
        return -1
    return 0


TREND_LABELS = {2: "strong bullish", 1: "bullish", 0: "neutral", -1: "bearish", -2: "strong bearish"}
