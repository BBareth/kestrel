from __future__ import annotations

from datetime import UTC, datetime, timedelta

import numpy as np
import pytest

from app.backtest.engine import run_backtest
from app.core import params as P
from app.performance.metrics import TradeRow, compute
from tests.helpers import BASE_MS


def _rows(tf_ms: int, n: int, seed: int) -> list[list]:
    rng = np.random.default_rng(seed)
    px = 80_000.0
    out = []
    for i in range(n):
        o = px
        px = max(1000.0, px * (1 + rng.normal(0, 0.0015)))
        h = max(o, px) * (1 + abs(rng.normal(0, 0.0006)))
        lo = min(o, px) * (1 - abs(rng.normal(0, 0.0006)))
        out.append([BASE_MS + i * tf_ms, o, h, lo, px, abs(rng.normal(100, 30)) + 1])
    return out


def test_backtest_runs_and_reports_consistently():
    n5 = 4000
    data = {"5m": _rows(300_000, n5, 1), "15m": _rows(900_000, n5 // 3, 2), "1h": _rows(3_600_000, n5 // 12, 3),
            "4h": _rows(14_400_000, n5 // 48, 4)}
    start = BASE_MS + 600 * 300_000
    end = BASE_MS + n5 * 300_000
    funding = [(BASE_MS + k * 28_800_000, 0.0001) for k in range(n5 * 300_000 // 28_800_000)]
    p = P.defaults()
    p["min_level_touches"] = 1  # random walk: make setups likelier so the loop is exercised
    p["volume_threshold"] = 1.0
    res = run_backtest(data, funding, p, start, end, 10_000.0)
    m = res["metrics"]
    assert res["counts"]["bars"] > 3000
    assert m["trades"] == len(res["trades"])
    assert res["final_equity"] == pytest.approx(10_000 + m["pnl"], abs=0.05)
    assert any("Past performance" in w for w in res["warnings"])
    for t in res["trades"]:
        assert t["opened_at"] <= t["closed_at"]


def test_metrics_math():
    t0 = datetime(2026, 9, 1, tzinfo=UTC)
    rows = [TradeRow("LONG", t0, t0 + timedelta(hours=1), 100, 2, 0, 2.0),
            TradeRow("SHORT", t0, t0 + timedelta(hours=2), -50, 2, -1, -1.0),
            TradeRow("LONG", t0, t0 + timedelta(hours=3), -50, 2, 0, -1.0),
            TradeRow("SHORT", t0, t0 + timedelta(days=1), 200, 2, 1, 3.0)]
    m = compute(rows, 10_000)
    assert m["trades"] == 4 and m["pnl"] == 200
    assert m["win_rate"] == 50.0 and m["profit_factor"] == 3.0
    assert m["max_drawdown_usdt"] == -100 and m["max_drawdown_pct"] == pytest.approx(-0.99, abs=0.01)
    assert m["long"]["trades"] == 2 and m["short"]["pnl"] == 150
    assert m["fees"] == 8 and m["funding"] == 0
    assert m["best_trade"]["pnl"] == 200 and m["worst_trade"]["pnl"] == -50


def test_metrics_empty():
    m = compute([], 10_000)
    assert m["trades"] == 0 and m["win_rate"] is None and m["profit_factor"] is None
