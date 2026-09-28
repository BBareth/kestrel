"""Builds the structured (not screenshot) market context sent to the AI layer."""

from __future__ import annotations

from typing import Any

from app.db import models as M
from app.exchange.models import AccountInfo, PositionInfo
from app.market.features import TFView
from app.strategy.base import Evaluation, MarketExtras


def _ohlcv(v: TFView, n: int) -> list[list[float]]:
    s = v.series
    lo = max(0, v.idx - n + 1)
    return [[int(s.open_time[i]), round(float(s.open[i]), 1), round(float(s.high[i]), 1), round(float(s.low[i]), 1),
             round(float(s.close[i]), 1), round(float(s.volume[i]), 2)] for i in range(lo, v.idx + 1)]


def build_context(ev: Evaluation, views: dict[str, TFView], extras: MarketExtras, snapshot: dict[str, Any],
                  account: AccountInfo | None, position: PositionInfo | None, recent: list[M.Trade],
                  risk: dict[str, Any] | None = None, mode: str = "paper") -> dict[str, Any]:
    ctx: dict[str, Any] = {
        "symbol": "BTCUSDT perpetual (Binance USDⓈ-M)",
        "trading_mode": mode,
        "time_utc_ms": ev.bar_time,
        "price": {
            "last": snapshot.get("price"), "mark": snapshot.get("mark_price"), "index": snapshot.get("index_price"),
            "change_24h_pct": snapshot.get("change_24h_pct"), "high_24h": snapshot.get("high_24h"),
            "low_24h": snapshot.get("low_24h"), "spread_bps": snapshot.get("spread_bps"),
        },
        "derivatives": {
            "funding_rate_pct": round(extras.funding_rate * 100, 5) if extras.funding_rate is not None else None,
            "open_interest_btc": extras.open_interest,
            "open_interest_change_1h_pct": extras.oi_change_1h_pct,
            "liquidations_1h": extras.liquidations_1h,
        },
        "deterministic_regime": ev.regime,
        "indicators": ev.timeframes,
        "support_resistance_5m": ev.levels,
        "ohlcv_columns": ["open_time_ms", "open", "high", "low", "close", "volume_btc"],
        "ohlcv": {"5m": _ohlcv(views["5m"], 36), "15m": _ohlcv(views["15m"], 16), "1h": _ohlcv(views["1h"], 24),
                  "4h": _ohlcv(views["4h"], 12)},
        "strategy_checklist": {"long": [c.to_dict() for c in ev.long_checks],
                               "short": [c.to_dict() for c in ev.short_checks]},
        "current_position": None,
        "recent_trades": [
            {"direction": t.direction, "entry": t.entry_price, "exit": t.exit_price, "pnl_usdt": round(t.realized_pnl, 2),
             "r": round(t.r_multiple, 2) if t.r_multiple is not None else None, "exit_reason": t.exit_reason,
             "closed_at": t.closed_at.isoformat() if t.closed_at else None}
            for t in recent
        ],
    }
    if account:
        ctx["account"] = {"equity_usdt": round(account.equity, 2), "available_usdt": round(account.available_balance, 2)}
    if position:
        ctx["current_position"] = {
            "direction": position.direction, "size_btc": position.size, "entry": position.entry_price,
            "mark": position.mark_price, "unrealized_pnl_usdt": round(position.unrealized_pnl, 2),
            "liquidation": position.liquidation_price, "leverage": position.leverage,
        }
    if ev.setup:
        s = ev.setup
        ctx["candidate"] = {
            "direction": s.direction, "entry": round(s.entry, 1), "stop": round(s.stop, 1), "tp1": round(s.tp1, 1),
            "tp2": round(s.tp2, 1), "reward_risk_blended": round(s.rr, 2), "level": round(s.level, 1),
            "strategy_confidence": round(s.confidence), "reasons": s.reasons, "warnings": s.warnings,
        }
    if risk:
        ctx["risk_engine"] = {"approved": risk.get("approved"), "sizing": risk.get("sizing"),
                              "note": "Risk engine already approved size/stop; you cannot change them."}
    return ctx
