"""Candle series + precomputed indicator features per timeframe."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from app.market import indicators as ind

INTERVAL_MS = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}
TIMEFRAMES = ("1m", "5m", "15m", "1h", "4h")


@dataclass
class Series:
    interval: str
    open_time: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray

    def __len__(self) -> int:
        return len(self.close)

    @property
    def close_time(self) -> np.ndarray:
        return self.open_time + INTERVAL_MS[self.interval] - 1

    @classmethod
    def from_rows(cls, interval: str, rows: list[Any]) -> Series:
        """Rows: Binance kline arrays or dicts with open_time/open/high/low/close/volume."""
        if rows and isinstance(rows[0], dict):
            ot = [r["open_time"] for r in rows]
            o = [r["open"] for r in rows]
            h = [r["high"] for r in rows]
            lo = [r["low"] for r in rows]
            c = [r["close"] for r in rows]
            v = [r["volume"] for r in rows]
        else:
            ot = [int(r[0]) for r in rows]
            o = [float(r[1]) for r in rows]
            h = [float(r[2]) for r in rows]
            lo = [float(r[3]) for r in rows]
            c = [float(r[4]) for r in rows]
            v = [float(r[5]) for r in rows]
        return cls(interval, np.array(ot, dtype=np.int64), np.array(o, dtype=float), np.array(h, dtype=float),
                   np.array(lo, dtype=float), np.array(c, dtype=float), np.array(v, dtype=float))

    def tail(self, n: int) -> Series:
        return Series(self.interval, self.open_time[-n:], self.open[-n:], self.high[-n:], self.low[-n:],
                      self.close[-n:], self.volume[-n:])

    def upto(self, n: int) -> Series:
        return Series(self.interval, self.open_time[:n], self.open[:n], self.high[:n], self.low[:n],
                      self.close[:n], self.volume[:n])


@dataclass
class Features:
    ema_fast: np.ndarray
    ema_mid: np.ndarray
    ema_slow: np.ndarray
    rsi: np.ndarray
    atr: np.ndarray
    vol_sma: np.ndarray
    vwap: np.ndarray
    mid_slope: np.ndarray
    pivot_idx: np.ndarray
    pivot_conf: np.ndarray
    pivot_price: np.ndarray
    pivot_high: np.ndarray  # bool


def compute_features(s: Series, p: dict[str, Any]) -> Features:
    pv = ind.pivots(s.high, s.low, int(p["pivot_strength"]))
    pv.sort(key=lambda x: (x.index, x.kind))
    return Features(
        ema_fast=ind.ema(s.close, int(p["ema_fast"])),
        ema_mid=ind.ema(s.close, int(p["ema_mid"])),
        ema_slow=ind.ema(s.close, int(p["ema_slow"])),
        rsi=ind.rsi(s.close, int(p["rsi_period"])),
        atr=ind.atr(s.high, s.low, s.close, int(p["atr_period"])),
        vol_sma=ind.sma(s.volume, int(p["volume_sma_period"])),
        vwap=ind.session_vwap(s.open_time, s.high, s.low, s.close, s.volume),
        mid_slope=ind.slope(ind.ema(s.close, int(p["ema_mid"])), 5),
        pivot_idx=np.array([x.index for x in pv], dtype=np.int64),
        pivot_conf=np.array([x.confirmed_at for x in pv], dtype=np.int64),
        pivot_price=np.array([x.price for x in pv], dtype=float),
        pivot_high=np.array([x.kind == "high" for x in pv], dtype=bool),
    )


@dataclass
class TFView:
    """A timeframe's series + features evaluated at bar ``idx`` (last *closed* bar usable)."""

    series: Series
    feat: Features
    idx: int

    def v(self, arr: np.ndarray, back: int = 0) -> float:
        i = self.idx - back
        if i < 0 or i >= len(arr):
            return float("nan")
        return float(arr[i])

    @property
    def close(self) -> float:
        return self.v(self.series.close)

    @property
    def atr(self) -> float:
        return self.v(self.feat.atr)

    def trend(self) -> int:
        return ind.trend_score(self.close, self.v(self.feat.ema_fast), self.v(self.feat.ema_mid),
                               self.v(self.feat.ema_slow), self.v(self.feat.mid_slope), self.atr)

    def pivots_between(self, start: int, end: int, known_by: int) -> list[ind.Pivot]:
        """Pivots whose extreme lies in [start, end] and that were confirmed by ``known_by``."""
        lo = int(np.searchsorted(self.feat.pivot_idx, start, side="left"))
        hi = int(np.searchsorted(self.feat.pivot_idx, end, side="right"))
        out = []
        for k in range(lo, hi):
            if self.feat.pivot_conf[k] <= known_by:
                out.append(ind.Pivot(int(self.feat.pivot_idx[k]), int(self.feat.pivot_conf[k]),
                                     float(self.feat.pivot_price[k]), "high" if self.feat.pivot_high[k] else "low"))
        return out

    def summary(self) -> dict[str, Any]:
        def r(x: float, nd: int = 2) -> float | None:
            return None if np.isnan(x) else round(x, nd)

        t = self.trend()
        return {
            "interval": self.series.interval,
            "close": r(self.close),
            "ema_fast": r(self.v(self.feat.ema_fast)),
            "ema_mid": r(self.v(self.feat.ema_mid)),
            "ema_slow": r(self.v(self.feat.ema_slow)),
            "rsi": r(self.v(self.feat.rsi), 1),
            "atr": r(self.atr),
            "atr_pct": r(self.atr / self.close * 100, 3) if self.close else None,
            "vwap": r(self.v(self.feat.vwap)),
            "volume_ratio": r(self.v(self.series.volume) / self.v(self.feat.vol_sma), 2)
            if self.v(self.feat.vol_sma) else None,
            "trend_score": t,
            "trend": ind.TREND_LABELS[t],
        }


def htf_index_at(htf: Series, time_ms: int) -> int:
    """Index of the last higher-timeframe bar fully closed at ``time_ms`` (−1 if none)."""
    ct = htf.close_time
    return int(np.searchsorted(ct, time_ms, side="right")) - 1
