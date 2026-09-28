"""Synthetic market data for deterministic strategy tests."""

from __future__ import annotations

import math

import numpy as np

from app.core import params as P
from app.market.features import INTERVAL_MS, Series, TFView, compute_features
from app.strategy.base import MarketExtras

BASE_MS = 1_790_000_000_000 - (1_790_000_000_000 % 14_400_000)


def _series(tf: str, o, h, lo, c, v, start_ms: int | None = None) -> Series:  # noqa: ANN001
    n = len(c)
    iv = INTERVAL_MS[tf]
    start = start_ms if start_ms is not None else BASE_MS
    return Series(tf, np.array([start + i * iv for i in range(n)], dtype=np.int64), np.array(o, float),
                  np.array(h, float), np.array(lo, float), np.array(c, float), np.array(v, float))


def breakout_retest_5m(volume_mult: float = 3.5, fail_retest: bool = False, bars_after: int = 3) -> Series:
    o, h, lo, c, v = [], [], [], [], []
    prev = 78000.0
    for i in range(200):  # steady uptrend with a small zig-zag
        close = 78000 + 25 * i + (15 if i % 2 else -15)
        o.append(prev)
        c.append(close)
        h.append(max(prev, close) + 40)
        lo.append(min(prev, close) - 40)
        v.append(100 + (i % 5))
        prev = close
    for k in range(62):  # range with 3 clean swing highs at 83400
        ph = k % 20
        close = 83200 + 150 * math.sin(2 * math.pi * ph / 20)
        o.append(prev)
        c.append(close)
        h.append(max(close + 50, prev + 5) if ph != 5 else 83400.0)
        lo.append(min(close - 50, prev - 5))
        v.append(100 + (k % 7))
        prev = close
    # breakout candle
    o.append(prev); c.append(83470); h.append(83480); lo.append(prev - 10); v.append(100 * volume_mult)
    # retest bars
    retest = [(83470, 83440, 83475, 83415), (83440, 83425, 83450, 83405)]
    if fail_retest:
        retest[1] = (83440, 83380, 83450, 83370)  # closes back below the level
    for ro, rc, rh, rl in retest[: max(0, bars_after - 1)]:
        o.append(ro); c.append(rc); h.append(rh); lo.append(rl); v.append(110)
    if bars_after >= 1:
        o.append(c[-1]); c.append(83470); h.append(83480); lo.append(83415); v.append(140)
    return _series("5m", o, h, lo, c, v)


def trend_series(tf: str, end: float, n: int = 250, step: float = 30.0, direction: int = 1) -> Series:
    closes = [end - direction * step * (n - 1 - i) + (8 if i % 2 else -8) for i in range(n)]
    o = [closes[0]] + closes[:-1]
    h = [max(a, b) + step * 0.6 for a, b in zip(o, closes, strict=True)]
    lo = [min(a, b) - step * 0.6 for a, b in zip(o, closes, strict=True)]
    return _series(tf, o, h, lo, closes, [1000.0] * n)


def mirror(s: Series, k: float = 166_000.0) -> Series:
    return Series(s.interval, s.open_time.copy(), k - s.open, k - s.low, k - s.high, k - s.close, s.volume.copy())


def views_for(s5: Series, htf_dir: int = 1, params: dict | None = None, s1h: Series | None = None,
              s4h: Series | None = None, s15: Series | None = None) -> dict[str, TFView]:
    p = params or P.defaults()
    end = float(s5.close[-1])
    series = {
        "1m": trend_series("1m", end, 300, 3, htf_dir),
        "5m": s5,
        "15m": s15 or trend_series("15m", end, 250, 20, htf_dir),
        "1h": s1h or trend_series("1h", end, 250, 60, htf_dir),
        "4h": s4h or trend_series("4h", end, 250, 150, htf_dir),
    }
    return {tf: TFView(s, compute_features(s, p), len(s) - 1) for tf, s in series.items()}


def extras(funding: float = 0.0001, oi_change: float | None = 0.8, spread: float = 0.2) -> MarketExtras:
    return MarketExtras(funding_rate=funding, next_funding_ms=None, open_interest=80_000, oi_change_1h_pct=oi_change,
                        spread_bps=spread, mark_price=None)
