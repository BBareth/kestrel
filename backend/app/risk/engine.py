"""Risk engine: the final authority on whether a trade may be opened and at what size.

Nothing upstream (strategy confidence, AI opinion) can override a failed check.
Position size is derived from risk, never from a fixed dollar amount:

    per_unit_risk = |entry − stop| + entry × (2 × taker_fee + stop_slippage)
    quantity      = equity × risk_per_trade ÷ per_unit_risk     (rounded DOWN to step)

then clipped by max notional, available margin and leverage. Rounding down means
the realised risk at the stop can only be *below* the configured budget.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.exchange.models import SymbolFilters
from app.exchange.paper import MAINT_MARGIN_RATE
from app.strategy.base import Check, Setup

# Absolute ceilings — independent of the (already validated) parameters.
HARD_MAX_RISK_PCT = 2.0
HARD_MAX_LEVERAGE = 10
HARD_MAX_DAILY_LOSS_PCT = 10.0


@dataclass
class AccountState:
    equity: float
    available: float
    wallet: float


@dataclass
class TradeStats:
    day_start_equity: float
    week_start_equity: float
    realized_today: float = 0.0
    realized_week: float = 0.0
    consecutive_losses: int = 0
    last_loss_at: datetime | None = None
    trades_last_hour: int = 0
    trades_today: int = 0
    open_positions: int = 0
    has_open_trade: bool = False


@dataclass
class SystemGates:
    kill_switch: bool = False
    strategy_enabled: bool = True
    halted: bool = False
    halt_reason: str | None = None
    guard_reasons: list[str] = field(default_factory=list)


@dataclass
class Sizing:
    quantity: float
    notional: float
    leverage: int
    margin: float
    risk_usdt: float
    risk_pct: float
    per_unit_risk: float
    liquidation_price: float
    tp1_qty: float
    tp2_qty: float
    split: bool

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 6) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class RiskDecision:
    approved: bool
    checks: list[Check]
    sizing: Sizing | None

    @property
    def blocked_by(self) -> list[str]:
        return [c.detail or c.name for c in self.checks if not c.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "checks": [c.to_dict() for c in self.checks],
            "sizing": self.sizing.to_dict() if self.sizing else None,
            "blocked_by": self.blocked_by,
        }


def isolated_liquidation_price(direction: str, entry: float, leverage: int) -> float:
    if direction == "LONG":
        return entry * (1 - 1 / leverage) / (1 - MAINT_MARGIN_RATE)
    return entry * (1 + 1 / leverage) / (1 + MAINT_MARGIN_RATE)


def _parse_hm(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def schedule_checks(p: dict[str, Any], now: datetime, next_funding_ms: int | None) -> list[Check]:
    out: list[Check] = []
    now = now.astimezone(UTC)
    minute = now.hour * 60 + now.minute
    start, end = _parse_hm(p["trading_start_utc"]), _parse_hm(p["trading_end_utc"])
    inside = start <= minute < end if start <= end else (minute >= start or minute < end)
    out.append(Check("trading window", inside,
                     f"{now:%H:%M} UTC {'inside' if inside else 'outside'} {p['trading_start_utc']}–{p['trading_end_utc']}"))
    if not p["weekend_trading"]:
        wk = now.weekday() >= 5
        out.append(Check("weekend", not wk, "weekend entries disabled" if wk else "weekday"))
    win = int(p["avoid_funding_window_min"])
    if win and next_funding_ms:
        mins = abs(next_funding_ms / 1000 - now.timestamp()) / 60
        # Funding every 8h (00/08/16 UTC) on BTCUSDT: also guard just *after* the previous one.
        since_prev = (now.timestamp() / 60) % 480
        ok = mins > win and since_prev > win
        out.append(Check("funding window", ok, f"{min(mins, since_prev):.0f} min from a funding timestamp"
                         + ("" if ok else f" (avoid ±{win} min)")))
    return out


class RiskEngine:
    def __init__(self, params: dict[str, Any]):
        self.p = params

    def size(self, setup: Setup, account: AccountState, filters: SymbolFilters) -> tuple[Sizing | None, list[Check]]:
        p = self.p
        checks: list[Check] = []
        entry, stop = setup.entry, setup.stop
        d = 1 if setup.direction == "LONG" else -1
        stop_dist = d * (entry - stop)
        if stop_dist <= 0:
            checks.append(Check("stop placement", False, "stop-loss is on the wrong side of entry"))
            return None, checks
        risk_pct = min(float(p["risk_per_trade_pct"]), HARD_MAX_RISK_PCT)
        leverage = int(min(int(p["max_leverage"]), HARD_MAX_LEVERAGE))
        if account.equity <= 0:
            checks.append(Check("equity", False, "account equity unavailable or zero"))
            return None, checks
        fee = p["taker_fee_pct"] / 100
        stop_slip = p["paper_stop_slippage_bps"] / 10_000
        per_unit = stop_dist + entry * (2 * fee + stop_slip)
        budget = account.equity * risk_pct / 100
        qty = budget / per_unit
        max_notional = min(float(p["max_position_notional_usdt"]), account.equity * leverage)
        margin_cap_notional = account.available * p["max_margin_usage_pct"] / 100 * leverage
        capped_by = None
        if qty * entry > max_notional:
            qty = max_notional / entry
            capped_by = "max position size"
        if qty * entry > margin_cap_notional:
            qty = margin_cap_notional / entry
            capped_by = "available margin"
        q = float(filters.round_qty_down(qty, market=True))
        min_qty = filters.min_qty_for(market=True)
        if q < min_qty:
            checks.append(Check("position size", False,
                                f"size {qty:.5f} BTC below exchange minimum {min_qty} — account too small for "
                                f"{risk_pct}% risk with this stop"))
            return None, checks
        notional = q * entry
        if notional < float(filters.min_notional):
            checks.append(Check("min notional", False, f"notional {notional:.2f} below exchange minimum "
                                                       f"{filters.min_notional} USDT"))
            return None, checks
        risk_usdt = q * per_unit
        margin = notional / leverage
        liq = isolated_liquidation_price(setup.direction, entry, leverage)
        frac = setup.tp1_fraction
        tp1_q = float(filters.round_qty_down(q * frac, market=True))
        tp2_q = float(filters.round_qty_down(q - tp1_q, market=True))
        split = tp1_q >= min_qty and tp2_q >= min_qty and abs(tp1_q + tp2_q - q) < 1e-9
        if not split:
            tp1_q, tp2_q = 0.0, q
        sizing = Sizing(quantity=q, notional=notional, leverage=leverage, margin=margin, risk_usdt=risk_usdt,
                        risk_pct=risk_usdt / account.equity * 100, per_unit_risk=per_unit, liquidation_price=liq,
                        tp1_qty=tp1_q, tp2_qty=tp2_q, split=split)
        detail = (f"{q} BTC ≈ {notional:,.0f} USDT at {leverage}× — risk {risk_usdt:.2f} USDT "
                  f"({sizing.risk_pct:.2f}% of equity)")
        if capped_by:
            detail += f", capped by {capped_by}"
        checks.append(Check("position size", True, detail))
        checks.append(Check("risk per trade", sizing.risk_pct <= risk_pct + 1e-6,
                            f"{sizing.risk_pct:.3f}% ≤ {risk_pct}%"))
        checks.append(Check("margin", margin <= account.available * p["max_margin_usage_pct"] / 100 + 1e-6,
                            f"margin {margin:,.2f} of {account.available:,.2f} available"))
        liq_mult = abs(entry - liq) / stop_dist
        checks.append(Check("liquidation buffer", liq_mult >= p["min_liq_distance_multiple"],
                            f"liquidation ≈ {liq:,.1f}, {liq_mult:.1f}× the stop distance away"))
        return sizing, checks

    def evaluate(self, setup: Setup, account: AccountState, stats: TradeStats, gates: SystemGates,
                 filters: SymbolFilters, funding_rate: float | None, spread_bps: float | None,
                 now: datetime, next_funding_ms: int | None = None) -> RiskDecision:
        p = self.p
        checks: list[Check] = []
        add = checks.append

        # System gates
        add(Check("kill switch", not gates.kill_switch, "kill switch engaged" if gates.kill_switch else "off"))
        add(Check("strategy enabled", gates.strategy_enabled,
                  "enabled" if gates.strategy_enabled else "strategy execution disabled"))
        add(Check("halt", not gates.halted, f"trading halted: {gates.halt_reason}" if gates.halted else "none"))
        add(Check("safety guards", not gates.guard_reasons,
                  "; ".join(gates.guard_reasons) if gates.guard_reasons else "all clear"))
        checks.extend(schedule_checks(p, now, next_funding_ms))

        # Mandatory stop + plan sanity
        d = 1 if setup.direction == "LONG" else -1
        has_stop = setup.stop is not None and setup.stop > 0 and d * (setup.entry - setup.stop) > 0
        add(Check("stop-loss", has_stop, f"stop {setup.stop:,.1f}" if has_stop else "missing/invalid stop-loss"))
        tp_ok = d * (setup.tp1 - setup.entry) > 0 and d * (setup.tp2 - setup.tp1) > 0
        add(Check("take-profit", tp_ok, f"TP1 {setup.tp1:,.1f} / TP2 {setup.tp2:,.1f}" if tp_ok else "invalid take-profit plan"))
        add(Check("min reward:risk", setup.rr >= p["min_rr"], f"R:R {setup.rr:.2f} (min {p['min_rr']})"))

        # Loss limits
        dl = -stats.realized_today / stats.day_start_equity * 100 if stats.day_start_equity > 0 else 0.0
        max_dl = min(p["max_daily_loss_pct"], HARD_MAX_DAILY_LOSS_PCT)
        add(Check("daily loss", dl < max_dl, f"today {-dl:+.2f}% (limit −{max_dl}%)"))
        wl = -stats.realized_week / stats.week_start_equity * 100 if stats.week_start_equity > 0 else 0.0
        add(Check("weekly loss", wl < p["max_weekly_loss_pct"], f"this week {-wl:+.2f}% (limit −{p['max_weekly_loss_pct']}%)"))

        # Streak + cooldowns
        if stats.last_loss_at is not None:
            since = now - stats.last_loss_at
            if stats.consecutive_losses >= p["max_consecutive_losses"]:
                cd = timedelta(minutes=p["cooldown_after_streak_min"])
                add(Check("losing streak", since >= cd,
                          f"{stats.consecutive_losses} losses in a row — paused until "
                          f"{(stats.last_loss_at + cd):%H:%M} UTC" if since < cd else
                          f"{stats.consecutive_losses} losses in a row, cooldown elapsed"))
            cd1 = timedelta(minutes=p["cooldown_after_loss_min"])
            add(Check("loss cooldown", since >= cd1,
                      f"last loss {since.total_seconds() / 60:.0f} min ago (cooldown {p['cooldown_after_loss_min']} min)"))
        # Frequency
        add(Check("trades per hour", stats.trades_last_hour < p["max_trades_per_hour"],
                  f"{stats.trades_last_hour} in the last hour (max {p['max_trades_per_hour']})"))
        add(Check("trades per day", stats.trades_today < p["max_trades_per_day"],
                  f"{stats.trades_today} today (max {p['max_trades_per_day']})"))
        # Exposure / duplicates
        add(Check("open positions", stats.open_positions < p["max_open_positions"],
                  f"{stats.open_positions} open (max {p['max_open_positions']})"))
        add(Check("duplicate", not stats.has_open_trade,
                  "a trade is already open or pending" if stats.has_open_trade else "no existing position"))
        # Market conditions
        if funding_rate is None:
            add(Check("funding", False, "funding rate unknown"))
        else:
            fr = abs(funding_rate) * 100
            add(Check("funding", fr <= p["max_funding_rate_pct"], f"|funding| {fr:.4f}% (max {p['max_funding_rate_pct']}%)"))
        if spread_bps is None:
            add(Check("spread", False, "order book unknown"))
        else:
            add(Check("spread", spread_bps <= p["max_spread_bps"], f"spread {spread_bps:.2f} bps (max {p['max_spread_bps']})"))

        sizing, size_checks = self.size(setup, account, filters)
        checks.extend(size_checks)
        approved = sizing is not None and all(c.passed for c in checks)
        return RiskDecision(approved=approved, checks=checks, sizing=sizing if approved else sizing)
