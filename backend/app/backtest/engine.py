"""Backtester using the *same* strategy and risk engine as live trading.

Fill model (deliberately conservative):
* Entries at the signal bar's close plus adverse slippage and taker fee.
* Stops / targets checked against each later bar's high/low. If one bar touches
  both the stop and a target, the STOP is assumed to fill first.
* Stops fill at the stop price minus stop slippage (or at the open if the bar
  gapped through it). Targets fill at the target minus normal slippage.
* Historical funding applied at every funding timestamp while a position is open.
* The AI layer is NOT simulated (it cannot be replayed faithfully); backtests show
  the deterministic strategy + risk engine only.
* Spread is unknown historically; a fixed assumption is used for the spread check.

Results are an estimate. Tuning parameters until a backtest looks good is
overfitting and says little about the future.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from app.core import params as P
from app.exchange.models import DEFAULT_BTCUSDT_FILTERS
from app.market.features import INTERVAL_MS, Series, TFView, compute_features
from app.performance.metrics import TradeRow, compute
from app.risk.engine import AccountState, RiskEngine, SystemGates, TradeStats, isolated_liquidation_price
from app.strategy.base import MarketExtras, StrategyState
from app.strategy.breakout_retest import BreakoutRetestStrategy

CONTEXT = {"5m": ("5m", "15m", "1h", "4h"), "15m": ("15m", "1h", "4h", "1d")}
ASSUMED_SPREAD_BPS = 0.5
FUNDING_INTERVAL_MS = 8 * 3_600_000

WARNING = ("Backtests use historical data and a simplified fill model. Past performance does not guarantee future "
           "results, and parameters tuned until a backtest looks good are usually overfitted.")


@dataclass
class OpenPos:
    direction: str
    entry: float
    qty: float
    remaining: float
    stop: float
    tp1: float | None
    tp2: float
    tp1_qty: float
    opened_ms: int
    risk_usdt: float
    equity_at_entry: float
    leverage: int
    liq: float
    fees: float = 0.0
    funding: float = 0.0
    realized: float = 0.0
    tp1_done: bool = False
    reasons: list[str] = field(default_factory=list)
    confidence: float = 0.0


def run_backtest(data: dict[str, list[list[Any]]], funding: list[tuple[int, float]], params: dict[str, Any],
                 start_ms: int, end_ms: int, starting_balance: float, primary: str = "5m",
                 progress=None) -> dict[str, Any]:  # noqa: ANN001
    """Pure CPU function (runs in a worker process). ``data[tf]`` = Binance kline rows."""
    t_start = time.time()
    p = P.enforce(params)
    tfs = CONTEXT[primary]
    series = {tf: Series.from_rows(tf, data[tf]) for tf in tfs}
    feats = {tf: compute_features(series[tf], p) for tf in tfs}
    close_times = {tf: series[tf].close_time for tf in tfs}
    prim = series[primary]
    n = len(prim)
    strat = BreakoutRetestStrategy()
    state = StrategyState()
    risk = RiskEngine(p)
    fee = p["taker_fee_pct"] / 100
    slip = p["paper_slippage_bps"] / 10_000
    stop_slip = p["paper_stop_slippage_bps"] / 10_000
    funding_arr = sorted(funding)
    f_times = np.array([f[0] for f in funding_arr], dtype=np.int64) if funding_arr else np.array([], dtype=np.int64)

    equity = starting_balance
    pos: OpenPos | None = None
    trades: list[dict[str, Any]] = []
    rows: list[TradeRow] = []
    counts = {"bars": 0, "setups": 0, "rejected_risk": 0, "entries": 0}
    reject_reasons: dict[str, int] = {}
    last_loss_ms: int | None = None
    streak = 0
    entry_times: list[int] = []
    day_start_eq: dict[str, float] = {}
    week_start_eq: dict[str, float] = {}
    realized_day: dict[str, float] = {}
    realized_week: dict[str, float] = {}

    def keys(ms: int) -> tuple[str, str]:
        d = datetime.fromtimestamp(ms / 1000, UTC)
        wk = (d - timedelta(days=d.weekday())).date().isoformat()
        return d.date().isoformat(), wk

    def close_pos(exit_px: float, qty: float, ms: int, reason: str, fee_rate: float) -> None:
        nonlocal equity, pos, streak, last_loss_ms
        if pos is None:
            return
        d = 1 if pos.direction == "LONG" else -1
        pnl = d * (exit_px - pos.entry) * qty
        f = qty * exit_px * fee_rate
        pos.realized += pnl
        pos.fees += f
        equity += pnl - f
        pos.remaining -= qty
        dk, wk = keys(ms)
        realized_day[dk] = realized_day.get(dk, 0.0) + pnl - f
        realized_week[wk] = realized_week.get(wk, 0.0) + pnl - f
        if pos.remaining <= 1e-9:
            net = pos.realized - pos.fees + pos.funding
            # entry fee was already deducted from equity at entry; include it in trade fees
            opened = datetime.fromtimestamp(pos.opened_ms / 1000, UTC)
            closed = datetime.fromtimestamp(ms / 1000, UTC)
            r = net / pos.risk_usdt if pos.risk_usdt else None
            trades.append({"direction": pos.direction, "entry": round(pos.entry, 2), "exit": round(exit_px, 2),
                           "qty": pos.qty, "opened_at": opened.isoformat(), "closed_at": closed.isoformat(),
                           "pnl": round(net, 2), "fees": round(pos.fees, 2), "funding": round(pos.funding, 2),
                           "r": round(r, 2) if r is not None else None, "exit_reason": reason,
                           "stop": round(pos.stop, 2), "tp2": round(pos.tp2, 2), "confidence": pos.confidence,
                           "reasons": pos.reasons[:6]})
            rows.append(TradeRow(pos.direction, opened, closed, net, pos.fees, pos.funding, r, pos.entry, exit_px,
                                 reason, len(rows) + 1))
            if net < 0:
                streak += 1
                last_loss_ms = ms
            else:
                streak = 0
            pos = None

    first = int(np.searchsorted(prim.open_time, start_ms, side="left"))
    first = max(first, int(p["ema_slow"]) + 50)
    last = int(np.searchsorted(prim.open_time, end_ms, side="right"))
    total_bars = max(1, last - first)
    for i in range(first, last):
        counts["bars"] += 1
        bar_open_ms = int(prim.open_time[i])
        bar_close_ms = int(prim.close_time[i])
        o, h, lo, c = float(prim.open[i]), float(prim.high[i]), float(prim.low[i]), float(prim.close[i])

        # 1) manage open position on this bar (entered on an earlier close)
        if pos is not None and pos.opened_ms < bar_open_ms:
            d = 1 if pos.direction == "LONG" else -1
            # funding timestamps inside this bar
            if len(f_times):
                a = int(np.searchsorted(f_times, bar_open_ms, side="left"))
                b = int(np.searchsorted(f_times, bar_close_ms, side="right"))
                for k in range(a, b):
                    amt = -d * pos.remaining * o * funding_arr[k][1]
                    pos.funding += amt
                    equity += amt
            if (d == 1 and lo <= pos.liq) or (d == -1 and h >= pos.liq):
                close_pos(pos.liq, pos.remaining, bar_close_ms, "liquidation", 0.005)
            else:
                stop_hit = (d == 1 and lo <= pos.stop) or (d == -1 and h >= pos.stop)
                if stop_hit:
                    base = min(pos.stop, o) if d == 1 else max(pos.stop, o)
                    px = base * (1 - d * (slip + stop_slip))
                    moved = pos.tp1_done and abs(pos.stop - pos.entry) / pos.entry < 0.005
                    close_pos(px, pos.remaining, bar_close_ms, "breakeven_stop" if moved else "stop_loss", fee)
                else:
                    if pos.tp1 is not None and not pos.tp1_done and ((d == 1 and h >= pos.tp1) or (d == -1 and lo <= pos.tp1)):
                        close_pos(pos.tp1 * (1 - d * slip), pos.tp1_qty, bar_close_ms, "tp1", fee)
                        if pos is not None:
                            pos.tp1_done = True
                            if p["breakeven_after_tp1"]:
                                pos.stop = pos.entry * (1 + d * 0.0012)
                    if pos is not None and ((d == 1 and h >= pos.tp2) or (d == -1 and lo <= pos.tp2)):
                        close_pos(pos.tp2 * (1 - d * slip), pos.remaining, bar_close_ms, "take_profit", fee)

        # 2) evaluate strategy at this bar's close when flat
        if pos is None:
            views = {}
            ok = True
            for slot, tf in zip(("5m", "15m", "1h", "4h"), tfs, strict=True):
                idx = i if tf == primary else int(np.searchsorted(close_times[tf], bar_close_ms, side="right")) - 1
                if idx < 30:
                    ok = False
                    break
                views[slot] = TFView(series[tf], feats[tf], idx)
            if not ok:
                continue
            fr = None
            if len(f_times):
                k = int(np.searchsorted(f_times, bar_close_ms, side="right")) - 1
                fr = funding_arr[k][1] if k >= 0 else None
            extras = MarketExtras(funding_rate=fr, spread_bps=ASSUMED_SPREAD_BPS, mark_price=c)
            ev = strat.evaluate(views, extras, p, state)
            if ev.setup is None:
                continue
            counts["setups"] += 1
            s = ev.setup
            state.last_signal_bar[s.direction] = ev.bar_time
            dk, wk = keys(bar_close_ms)
            day_start_eq.setdefault(dk, equity)
            week_start_eq.setdefault(wk, equity)
            now_dt = datetime.fromtimestamp(bar_close_ms / 1000, UTC)
            stats = TradeStats(
                day_start_equity=day_start_eq[dk], week_start_equity=week_start_eq[wk],
                realized_today=realized_day.get(dk, 0.0), realized_week=realized_week.get(wk, 0.0),
                consecutive_losses=streak,
                last_loss_at=datetime.fromtimestamp(last_loss_ms / 1000, UTC) if last_loss_ms else None,
                trades_last_hour=sum(1 for t in entry_times if bar_close_ms - t < 3_600_000),
                trades_today=sum(1 for t in entry_times if keys(t)[0] == dk))
            next_f = ((bar_close_ms // FUNDING_INTERVAL_MS) + 1) * FUNDING_INTERVAL_MS
            dec = risk.evaluate(s, AccountState(equity, equity, equity), stats, SystemGates(), DEFAULT_BTCUSDT_FILTERS,
                                fr if fr is not None else 0.0, ASSUMED_SPREAD_BPS, now_dt, next_f)
            if not dec.approved or dec.sizing is None:
                counts["rejected_risk"] += 1
                for b in dec.blocked_by[:1]:
                    key = b.split("(")[0].strip()[:60]
                    reject_reasons[key] = reject_reasons.get(key, 0) + 1
                continue
            sz = dec.sizing
            d = 1 if s.direction == "LONG" else -1
            entry_px = c * (1 + d * slip)
            entry_fee = sz.quantity * entry_px * fee
            equity -= entry_fee
            realized_day[dk] = realized_day.get(dk, 0.0) - entry_fee
            realized_week[wk] = realized_week.get(wk, 0.0) - entry_fee
            pos = OpenPos(direction=s.direction, entry=entry_px, qty=sz.quantity, remaining=sz.quantity, stop=s.stop,
                          tp1=s.tp1 if sz.split else None, tp2=s.tp2, tp1_qty=sz.tp1_qty, opened_ms=bar_close_ms,
                          risk_usdt=sz.risk_usdt, equity_at_entry=equity, leverage=sz.leverage,
                          liq=isolated_liquidation_price(s.direction, entry_px, sz.leverage), fees=entry_fee,
                          reasons=s.reasons, confidence=s.confidence)
            entry_times.append(bar_close_ms)
            counts["entries"] += 1
        if progress and (i - first) % 500 == 0:
            progress((i - first) / total_bars)

    if pos is not None:  # close at the last price so results are complete
        close_pos(float(prim.close[last - 1]), pos.remaining, int(prim.close_time[last - 1]), "end_of_test", fee)

    metrics = compute(rows, starting_balance)
    metrics.pop("equity_curve", None)
    curve = [[int(start_ms / 1000), starting_balance]]
    eq = starting_balance
    for r in rows:
        eq += r.pnl
        curve.append([int(r.closed_at.timestamp()), round(eq, 2)])
    return {
        "metrics": metrics,
        "equity_curve": curve,
        "trades": trades[-500:],
        "counts": counts,
        "rejections": dict(sorted(reject_reasons.items(), key=lambda kv: -kv[1])[:10]),
        "final_equity": round(equity, 2),
        "starting_balance": starting_balance,
        "primary_timeframe": primary,
        "elapsed_s": round(time.time() - t_start, 2),
        "warnings": [WARNING, "AI confirmation is not simulated in backtests.",
                     f"Spread assumed at {ASSUMED_SPREAD_BPS} bps; fees {p['taker_fee_pct']}% taker; "
                     f"slippage {p['paper_slippage_bps']} bps (+{p['paper_stop_slippage_bps']} bps on stops)."],
    }


def warmup_ms(tf: str, bars: int = 600) -> int:
    return INTERVAL_MS[tf] * bars
