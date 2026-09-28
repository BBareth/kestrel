"""Performance statistics shared by the live dashboard and the backtester."""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


@dataclass
class TradeRow:
    direction: str
    opened_at: datetime
    closed_at: datetime
    pnl: float  # net
    fees: float
    funding: float
    r: float | None = None
    entry: float | None = None
    exit: float | None = None
    exit_reason: str | None = None
    id: int | None = None


def _side_stats(rows: list[TradeRow]) -> dict[str, Any]:
    wins = [t.pnl for t in rows if t.pnl > 0]
    losses = [t.pnl for t in rows if t.pnl <= 0]
    gross_win, gross_loss = sum(wins), -sum(losses)
    return {
        "trades": len(rows),
        "pnl": round(sum(t.pnl for t in rows), 2),
        "win_rate": round(len(wins) / len(rows) * 100, 1) if rows else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
    }


def compute(trades: list[TradeRow], starting_equity: float, equity_points: list[tuple[datetime, float]] | None = None
            ) -> dict[str, Any]:
    trades = sorted(trades, key=lambda t: t.closed_at)
    n = len(trades)
    wins = [t for t in trades if t.pnl > 0]
    losses = [t for t in trades if t.pnl <= 0]
    gross_win = sum(t.pnl for t in wins)
    gross_loss = -sum(t.pnl for t in losses)
    total = sum(t.pnl for t in trades)

    # Equity curve from realised P&L (plus optional sampled equity for unrealised swings)
    curve: list[tuple[int, float]] = []
    eq = starting_equity
    peak = eq
    max_dd = 0.0
    max_dd_abs = 0.0
    dd_curve: list[tuple[int, float]] = []
    if trades:
        curve.append((int(trades[0].opened_at.timestamp()), eq))
        dd_curve.append((int(trades[0].opened_at.timestamp()), 0.0))
    for t in trades:
        eq += t.pnl
        peak = max(peak, eq)
        dd = (eq - peak) / peak * 100 if peak > 0 else 0.0
        max_dd = min(max_dd, dd)
        max_dd_abs = min(max_dd_abs, eq - peak)
        ts = int(t.closed_at.timestamp())
        curve.append((ts, round(eq, 2)))
        dd_curve.append((ts, round(dd, 3)))
    if equity_points:
        pk = equity_points[0][1]
        for _, v in equity_points:
            pk = max(pk, v)
            if pk > 0:
                max_dd = min(max_dd, (v - pk) / pk * 100)

    by_day: dict[str, float] = defaultdict(float)
    for t in trades:
        by_day[t.closed_at.astimezone(UTC).date().isoformat()] += t.pnl
    daily = [by_day[k] for k in sorted(by_day)]
    sharpe = None
    if len(daily) >= 5:
        mean = sum(daily) / len(daily)
        var = sum((x - mean) ** 2 for x in daily) / (len(daily) - 1)
        sd = math.sqrt(var)
        if sd > 0:
            sharpe = round(mean / sd * math.sqrt(365), 2)

    rs = [t.r for t in trades if t.r is not None]
    # Ordered list (JSONB would reorder dict keys): buckets of 0.5R, open-ended at both ends.
    edges = [-1.5, -1.0, -0.5, 0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    labels = ["< −1.5R"] + [f"{a:+.1f}…{b:+.1f}R" for a, b in zip(edges, edges[1:], strict=False)] + ["≥ +3.0R"]
    counts = [0] * len(labels)
    for r in rs:
        k = sum(1 for e in edges if r >= e)
        counts[k] += 1
    dist = [{"bucket": lb, "count": n, "negative": i <= 3} for i, (lb, n) in enumerate(zip(labels, counts, strict=True))]

    best = max(trades, key=lambda t: t.pnl) if trades else None
    worst = min(trades, key=lambda t: t.pnl) if trades else None
    pf = gross_win / gross_loss if gross_loss > 0 else None
    return {
        "trades": n,
        "pnl": round(total, 2),
        "roi_pct": round(total / starting_equity * 100, 3) if starting_equity else None,
        "win_rate": round(len(wins) / n * 100, 1) if n else None,
        "loss_rate": round(len(losses) / n * 100, 1) if n else None,
        "profit_factor": round(pf, 2) if pf is not None else None,
        "max_drawdown_pct": round(max_dd, 2),
        "max_drawdown_usdt": round(max_dd_abs, 2),
        "avg_trade": round(total / n, 2) if n else None,
        "avg_win": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "expectancy_r": round(sum(rs) / len(rs), 3) if rs else None,
        "fees": round(sum(t.fees for t in trades), 2),
        "funding": round(sum(t.funding for t in trades), 2),
        "sharpe_like": sharpe,
        "best_trade": {"id": best.id, "pnl": round(best.pnl, 2), "direction": best.direction} if best else None,
        "worst_trade": {"id": worst.id, "pnl": round(worst.pnl, 2), "direction": worst.direction} if worst else None,
        "long": _side_stats([t for t in trades if t.direction == "LONG"]),
        "short": _side_stats([t for t in trades if t.direction == "SHORT"]),
        "equity_curve": curve,
        "drawdown_curve": dd_curve,
        "pnl_by_day": [{"day": k, "pnl": round(by_day[k], 2)} for k in sorted(by_day)],
        "r_distribution": dist,
        "exit_reasons": _count(t.exit_reason or "unknown" for t in trades),
    }


def _count(items) -> dict[str, int]:  # noqa: ANN001
    out: dict[str, int] = defaultdict(int)
    for i in items:
        out[i] += 1
    return dict(out)
