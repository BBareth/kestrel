"""Parameter catalogue for strategy, risk, AI, schedule and execution settings.

Each parameter declares its default, its UI range and — separately — a *hard cap*
enforced in code. The UI can move a value anywhere inside the range; nothing
(not the UI, not the AI, not a hand-edited database row) can push a value past
the hard cap, because every consumer reads parameters through ``validate``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

STRATEGY_NAME = "breakout_retest_v1"


@dataclass(frozen=True)
class ParamSpec:
    key: str
    group: str
    label: str
    type: str  # int | float | bool | enum | str | time
    default: Any
    help: str
    min: float | None = None
    max: float | None = None
    step: float | None = None
    unit: str = ""
    options: tuple[str, ...] = ()
    locked: bool = False  # displayed but not editable (safety invariant)
    risk_sensitive: bool = False  # changing it while LIVE requires re-authentication

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["options"] = list(self.options)
        return d


def _p(*args: Any, **kw: Any) -> ParamSpec:
    return ParamSpec(*args, **kw)


PARAMS: list[ParamSpec] = [
    # ------------------------------------------------------------------ strategy
    _p("ema_fast", "strategy", "EMA fast", "int", 7, "Fast EMA on each timeframe. Used for short-term momentum (fast above mid = bullish momentum).", 2, 50, 1),
    _p("ema_mid", "strategy", "EMA mid", "int", 25, "Medium EMA. Trend structure and slope are measured on this line.", 5, 100, 1),
    _p("ema_slow", "strategy", "EMA slow", "int", 99, "Slow EMA. Price above/below it defines the dominant trend on a timeframe.", 20, 300, 1),
    _p("rsi_period", "strategy", "RSI period", "int", 14, "Wilder RSI look-back.", 5, 50, 1),
    _p("rsi_long_min", "strategy", "RSI long min", "float", 50, "LONG entries need 5m RSI at or above this (momentum present).", 30, 70, 1),
    _p("rsi_long_max", "strategy", "RSI long max", "float", 72, "LONG entries are skipped above this RSI (too stretched to chase).", 55, 90, 1),
    _p("rsi_short_min", "strategy", "RSI short min", "float", 28, "SHORT entries are skipped below this RSI (too stretched to chase).", 10, 45, 1),
    _p("rsi_short_max", "strategy", "RSI short max", "float", 50, "SHORT entries need 5m RSI at or below this.", 30, 70, 1),
    _p("atr_period", "strategy", "ATR period", "int", 14, "Average True Range look-back. Stops, tolerances and volatility filters are expressed in ATR.", 5, 50, 1),
    _p("volume_sma_period", "strategy", "Volume average", "int", 20, "Bars used for the average volume that breakouts are compared against.", 5, 100, 1),
    _p("volume_threshold", "strategy", "Breakout volume ×", "float", 1.3, "Breakout candle volume must be at least this multiple of average volume.", 1.0, 5.0, 0.05, "×"),
    _p("pivot_strength", "strategy", "Swing strength", "int", 3, "Bars on each side that a swing high/low must dominate. A swing is only known this many bars after it formed (no look-ahead).", 2, 10, 1),
    _p("level_lookback_bars", "strategy", "Level look-back", "int", 96, "5m bars scanned for support/resistance swings (96 = 8 hours).", 24, 600, 1, "bars"),
    _p("level_cluster_atr", "strategy", "Level clustering", "float", 0.3, "Swings closer than this many ATR are merged into one level; each merge counts as a touch.", 0.05, 2.0, 0.05, "ATR"),
    _p("min_level_touches", "strategy", "Min level touches", "int", 2, "A level is 'meaningful' with at least this many touches, or if it is the extreme swing of the look-back.", 1, 5, 1),
    _p("breakout_max_age_bars", "strategy", "Breakout max age", "int", 12, "The breakout must have happened within this many 5m bars; older breakouts are stale.", 2, 48, 1, "bars"),
    _p("breakout_buffer_atr", "strategy", "Breakout buffer", "float", 0.1, "The breakout candle must close beyond the level by at least this many ATR.", 0.0, 1.0, 0.05, "ATR"),
    _p("retest_tolerance_atr", "strategy", "Retest tolerance", "float", 0.3, "After the breakout, price must pull back to within this many ATR of the level.", 0.05, 1.5, 0.05, "ATR"),
    _p("retest_max_penetration_atr", "strategy", "Retest max pierce", "float", 0.35, "A retest wick may pierce back through the level by at most this many ATR; candle closes must hold.", 0.0, 1.5, 0.05, "ATR"),
    _p("max_entry_extension_atr", "strategy", "Max chase", "float", 1.0, "Entry is skipped if price already ran more than this many ATR past the level.", 0.2, 3.0, 0.1, "ATR"),
    _p("stop_buffer_atr", "strategy", "Stop buffer", "float", 0.25, "Stop sits this many ATR beyond the retest extreme.", 0.0, 2.0, 0.05, "ATR"),
    _p("min_stop_atr", "strategy", "Min stop distance", "float", 0.6, "Stops tighter than this (in ATR) are widened — noise would take them out.", 0.2, 3.0, 0.1, "ATR"),
    _p("max_stop_atr", "strategy", "Max stop distance", "float", 2.5, "Setups needing a wider stop than this are rejected.", 0.5, 6.0, 0.1, "ATR"),
    _p("tp1_r", "strategy", "TP1 (R)", "float", 1.5, "First take-profit, in multiples of the stop distance (R).", 0.5, 5.0, 0.1, "R"),
    _p("tp2_r", "strategy", "TP2 (R)", "float", 3.0, "Final take-profit in R. Capped at the next opposing higher-timeframe level if closer.", 1.0, 10.0, 0.1, "R"),
    _p("tp1_close_fraction", "strategy", "TP1 size", "float", 0.5, "Fraction of the position closed at TP1.", 0.1, 0.9, 0.05),
    _p("breakeven_after_tp1", "strategy", "Breakeven after TP1", "bool", True, "After TP1 fills, the stop on the remainder moves to entry (plus fees)."),
    _p("min_atr_pct", "strategy", "Min volatility", "float", 0.05, "Skip dead markets: 5m ATR must be at least this % of price.", 0.0, 1.0, 0.01, "%"),
    _p("max_atr_pct", "strategy", "Max volatility", "float", 1.2, "Skip chaotic markets: 5m ATR above this % of price blocks entries.", 0.2, 5.0, 0.05, "%"),
    _p("long_min_1h_trend", "strategy", "LONG: min 1h trend", "int", 0, "Trend score runs −2 (strong bear) … +2 (strong bull). LONG needs the 1h score at or above this.", -2, 2, 1),
    _p("long_min_4h_trend", "strategy", "LONG: min 4h trend", "int", -1, "LONG needs the 4h trend score at or above this (−1 = 'not strongly bearish').", -2, 2, 1),
    _p("short_max_1h_trend", "strategy", "SHORT: max 1h trend", "int", 0, "SHORT needs the 1h trend score at or below this.", -2, 2, 1),
    _p("short_max_4h_trend", "strategy", "SHORT: max 4h trend", "int", 1, "SHORT needs the 4h trend score at or below this (+1 = 'not strongly bullish').", -2, 2, 1),
    _p("require_15m_alignment", "strategy", "15m must agree", "bool", True, "Require the 15m trend to be neutral or in the trade's direction."),
    _p("min_confidence", "strategy", "Min confidence", "float", 60, "Deterministic setup score (0–100) required before a signal goes to risk checks.", 0, 100, 1, "%"),
    _p("signal_cooldown_bars", "strategy", "Signal cooldown", "int", 6, "After a signal in one direction, the same direction is not signalled again for this many bars.", 0, 100, 1, "bars"),
    _p("crowded_funding_pct", "strategy", "Crowded funding", "float", 0.03, "Funding beyond this % against the trade direction lowers confidence (crowded positioning).", 0.0, 0.5, 0.005, "%"),
    # ------------------------------------------------------------------ risk
    _p("risk_per_trade_pct", "risk", "Risk per trade", "float", 0.5, "Loss at the stop, as % of account equity. Position size = equity × risk ÷ stop distance.", 0.05, 2.0, 0.05, "%", risk_sensitive=True),
    _p("max_leverage", "risk", "Max leverage", "int", 3, "Leverage set on the exchange (isolated margin). Hard cap 10×.", 1, 10, 1, "×", risk_sensitive=True),
    _p("max_position_notional_usdt", "risk", "Max position size", "float", 10000, "Upper bound on position notional in USDT, whatever the sizing formula says.", 100, 1_000_000, 100, "USDT", risk_sensitive=True),
    _p("max_margin_usage_pct", "risk", "Max margin usage", "float", 50, "Initial margin for a new position may use at most this % of available balance.", 5, 90, 5, "%", risk_sensitive=True),
    _p("max_daily_loss_pct", "risk", "Max daily loss", "float", 2.0, "Realised loss today (UTC) that blocks new trades until tomorrow.", 0.5, 10.0, 0.25, "%", risk_sensitive=True),
    _p("max_weekly_loss_pct", "risk", "Max weekly loss", "float", 5.0, "Realised loss this ISO week that blocks new trades until next week.", 1.0, 20.0, 0.5, "%", risk_sensitive=True),
    _p("max_consecutive_losses", "risk", "Max losing streak", "int", 3, "After this many losses in a row, trading pauses for the streak cooldown.", 1, 10, 1, risk_sensitive=True),
    _p("cooldown_after_loss_min", "risk", "Cooldown after loss", "int", 30, "Minutes without new entries after any losing trade.", 0, 1440, 5, "min", risk_sensitive=True),
    _p("cooldown_after_streak_min", "risk", "Cooldown after streak", "int", 240, "Minutes without new entries after hitting the losing-streak limit.", 15, 4320, 15, "min", risk_sensitive=True),
    _p("max_trades_per_hour", "risk", "Max trades / hour", "int", 2, "Entries allowed in any rolling 60 minutes.", 1, 10, 1, risk_sensitive=True),
    _p("max_trades_per_day", "risk", "Max trades / day", "int", 6, "Entries allowed per UTC day.", 1, 50, 1, risk_sensitive=True),
    _p("max_open_positions", "risk", "Max open positions", "int", 1, "BTCUSDT in one-way mode can hold one position, so this is fixed at 1.", 1, 1, 1, locked=True),
    _p("min_rr", "risk", "Min reward:risk", "float", 2.0, "Blended reward:risk of the TP plan must be at least this.", 1.0, 10.0, 0.1, "R", risk_sensitive=True),
    _p("max_funding_rate_pct", "risk", "Max funding", "float", 0.05, "No entries while |funding rate| exceeds this % per interval.", 0.005, 0.5, 0.005, "%", risk_sensitive=True),
    _p("max_spread_bps", "risk", "Max spread", "float", 3.0, "No entries while the bid/ask spread is wider than this (basis points).", 0.5, 50, 0.5, "bps", risk_sensitive=True),
    _p("max_slippage_bps", "risk", "Max slippage", "float", 8.0, "Entry is aborted if price moved more than this from the signal price before submission.", 1, 100, 1, "bps", risk_sensitive=True),
    _p("min_liq_distance_multiple", "risk", "Liquidation buffer", "float", 3.0, "Liquidation price must be at least this many stop-distances away from entry.", 1.5, 20, 0.5, "×", risk_sensitive=True),
    _p("require_stop_loss", "risk", "Mandatory stop-loss", "bool", True, "Every position gets an exchange-side stop-loss. This cannot be turned off.", locked=True),
    # ------------------------------------------------------------------ ai
    _p("ai_mode", "ai", "AI confirmation", "enum", "required", "off: never call the model. advisory: analyse and record, never gate. required: the model must agree with the direction at or above the confidence threshold.", options=("off", "advisory", "required")),
    _p("ai_min_confidence", "ai", "AI min confidence", "float", 65, "In 'required' mode, the model's confidence must reach this.", 0, 100, 1, "%"),
    _p("ai_unavailable_policy", "ai", "If AI unavailable", "enum", "no_trade", "What to do when the model errors, times out or returns invalid JSON: refuse the trade, or continue strategy-only.", options=("no_trade", "deterministic")),
    _p("ai_budget_policy", "ai", "If budget exceeded", "enum", "no_trade", "What to do once today's AI budget is spent.", options=("no_trade", "deterministic")),
    _p("ai_daily_budget_usd", "ai", "Daily AI budget", "float", 1.0, "Estimated OpenAI spend per UTC day before the budget policy applies.", 0.0, 50.0, 0.25, "USD"),
    _p("ai_model", "ai", "Model", "str", "gpt-6-sol", "OpenAI model id used for analysis."),
    _p("ai_reasoning_effort", "ai", "Reasoning effort", "enum", "low", "Higher effort costs more tokens and time.", options=("none", "low", "medium", "high")),
    _p("ai_timeout_s", "ai", "Timeout", "int", 60, "Seconds before an AI request counts as unavailable.", 10, 180, 5, "s"),
    _p("ai_regime_trigger", "ai", "Analyse on regime change", "bool", True, "Call the model when the deterministic regime classifier changes state."),
    _p("ai_regime_cooldown_min", "ai", "Regime analysis cooldown", "int", 60, "Minimum minutes between regime-change analyses.", 10, 1440, 5, "min"),
    _p("ai_position_review_min", "ai", "Position review every", "int", 60, "Advisory review of an open position every N minutes (0 = off). Advice is shown, never executed.", 0, 1440, 5, "min"),
    _p("ai_periodic_min", "ai", "Periodic analysis every", "int", 240, "Background market read every N minutes (0 = off).", 0, 1440, 15, "min"),
    # ------------------------------------------------------------------ schedule
    _p("trading_start_utc", "schedule", "Trading window start", "time", "00:00", "New entries only inside the daily UTC window. Open positions are always managed."),
    _p("trading_end_utc", "schedule", "Trading window end", "time", "24:00", "End of the UTC entry window (24:00 = end of day)."),
    _p("avoid_funding_window_min", "schedule", "Avoid funding ±", "int", 10, "No entries within this many minutes of a funding timestamp.", 0, 120, 5, "min"),
    _p("weekend_trading", "schedule", "Trade weekends", "bool", True, "Allow entries on Saturday/Sunday (UTC), when liquidity is thinner."),
    # ------------------------------------------------------------------ execution
    _p("taker_fee_pct", "execution", "Taker fee", "float", 0.05, "Fee assumed for market orders in paper trading and backtests.", 0.0, 0.2, 0.005, "%"),
    _p("paper_slippage_bps", "execution", "Paper slippage", "float", 2.0, "Adverse slippage applied to paper market fills.", 0, 50, 0.5, "bps"),
    _p("paper_stop_slippage_bps", "execution", "Paper stop slippage", "float", 5.0, "Extra adverse slippage for paper stop-loss fills (stops fill in fast markets).", 0, 100, 0.5, "bps"),
    _p("tp_failure_policy", "execution", "If TP can't be placed", "enum", "close", "close: flatten the position if a take-profit cannot be placed and verified. keep: keep the stop-protected position and manage the TP in software.", options=("close", "keep")),
]

SAFETY_PARAMS: list[ParamSpec] = [
    _p("kill_positions", "safety", "Kill switch: open positions", "enum", "close", "close: flatten positions at market. keep_protected: leave them with their exchange stop-loss and take-profit in place.", options=("close", "keep_protected")),
    _p("kill_cancel_orders", "safety", "Kill switch: cancel orders", "bool", True, "Cancel open orders (protective orders are only cancelled once the position is flat)."),
    _p("kill_to_paper", "safety", "Kill switch returns to paper", "bool", True, "Engaging the kill switch in LIVE mode switches back to PAPER; live needs the full unlock again."),
    _p("stale_data_seconds", "safety", "Stale market data", "int", 30, "Market data older than this pauses new entries.", 5, 300, 5, "s"),
    _p("stale_halt_minutes", "safety", "Stale data → halt", "int", 10, "Data stale for longer than this halts trading until re-enabled.", 1, 120, 1, "min"),
    _p("max_clock_skew_ms", "safety", "Max clock skew", "int", 1000, "Local vs Binance clock difference that pauses trading.", 100, 5000, 100, "ms"),
    _p("api_error_threshold", "safety", "API error threshold", "int", 8, "Exchange API errors within 5 minutes that halt trading.", 2, 100, 1),
    _p("unexpected_position_policy", "safety", "Unknown position", "enum", "protect", "When the exchange shows a position Kestrel did not open: protect = adopt it and make sure it has a stop-loss; flatten = close it; halt_only = only halt and alert.", options=("protect", "flatten", "halt_only")),
    _p("emergency_stop_pct", "safety", "Emergency stop distance", "float", 1.5, "Distance of the stop placed on a recovered/unknown position, % from mark.", 0.3, 10, 0.1, "%"),
    _p("min_paper_trades_for_live", "safety", "Paper trades before live", "int", 10, "Closed paper trades required by the live-readiness checklist.", 0, 500, 1),
    _p("min_paper_days_for_live", "safety", "Paper days before live", "int", 7, "Days of paper trading required by the live-readiness checklist.", 0, 90, 1, "days"),
]

_BY_KEY = {p.key: p for p in PARAMS}
_SAFETY_BY_KEY = {p.key: p for p in SAFETY_PARAMS}


def defaults() -> dict[str, Any]:
    return {p.key: p.default for p in PARAMS}


def safety_defaults() -> dict[str, Any]:
    return {p.key: p.default for p in SAFETY_PARAMS}


def _coerce(spec: ParamSpec, value: Any) -> tuple[Any, str | None]:
    if spec.locked:
        return spec.default, None
    try:
        if spec.type == "int":
            if isinstance(value, bool):
                raise ValueError
            v = int(round(float(value)))
        elif spec.type == "float":
            if isinstance(value, bool):
                raise ValueError
            v = float(value)
            if v != v or v in (float("inf"), float("-inf")):
                raise ValueError
        elif spec.type == "bool":
            if isinstance(value, bool):
                v = value
            elif str(value).lower() in ("true", "1", "yes", "on"):
                v = True
            elif str(value).lower() in ("false", "0", "no", "off"):
                v = False
            else:
                raise ValueError
        elif spec.type == "enum":
            v = str(value)
            if v not in spec.options:
                return spec.default, f"{spec.label}: must be one of {', '.join(spec.options)}"
        elif spec.type == "time":
            v = str(value)
            hh, mm = v.split(":")
            h, m = int(hh), int(mm)
            if not (0 <= h <= 24 and 0 <= m < 60) or (h == 24 and m != 0):
                raise ValueError
            v = f"{h:02d}:{m:02d}"
        else:
            v = str(value).strip()
            if not v or len(v) > 64:
                raise ValueError
    except (TypeError, ValueError):
        return spec.default, f"{spec.label}: invalid value {value!r}"
    if spec.type in ("int", "float"):
        if spec.min is not None and v < spec.min:
            return spec.default, f"{spec.label}: minimum is {spec.min}"
        if spec.max is not None and v > spec.max:
            return spec.default, f"{spec.label}: maximum is {spec.max}"
    return v, None


def _validate(specs: dict[str, ParamSpec], params: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    params = params or {}
    clean: dict[str, Any] = {}
    errors: list[str] = []
    for key, spec in specs.items():
        if key in params:
            v, err = _coerce(spec, params[key])
            if err:
                errors.append(err)
            clean[key] = v
        else:
            clean[key] = spec.default
    unknown = set(params) - set(specs)
    for k in sorted(unknown):
        errors.append(f"unknown parameter {k!r}")
    return clean, errors


def validate(params: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    clean, errors = _validate(_BY_KEY, params)
    # Cross-field invariants
    if clean["ema_fast"] >= clean["ema_mid"] or clean["ema_mid"] >= clean["ema_slow"]:
        errors.append("EMA periods must satisfy fast < mid < slow")
    if clean["rsi_long_min"] >= clean["rsi_long_max"]:
        errors.append("RSI long min must be below RSI long max")
    if clean["rsi_short_min"] >= clean["rsi_short_max"]:
        errors.append("RSI short min must be below RSI short max")
    if clean["min_stop_atr"] >= clean["max_stop_atr"]:
        errors.append("Min stop distance must be below max stop distance")
    if clean["tp1_r"] >= clean["tp2_r"]:
        errors.append("TP1 must be closer than TP2")
    if clean["min_atr_pct"] >= clean["max_atr_pct"]:
        errors.append("Min volatility must be below max volatility")
    if clean["cooldown_after_streak_min"] < clean["cooldown_after_loss_min"]:
        errors.append("Streak cooldown must be at least the single-loss cooldown")
    if clean["max_weekly_loss_pct"] < clean["max_daily_loss_pct"]:
        errors.append("Weekly loss limit must be at least the daily loss limit")
    return clean, errors


def validate_safety(params: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    return _validate(_SAFETY_BY_KEY, params)


def enforce(params: dict[str, Any] | None) -> dict[str, Any]:
    """Return params guaranteed inside the hard caps (invalid values → defaults)."""
    clean, _ = validate(params)
    return clean


def catalogue() -> dict[str, Any]:
    return {
        "strategy_name": STRATEGY_NAME,
        "params": [p.to_dict() for p in PARAMS],
        "safety": [p.to_dict() for p in SAFETY_PARAMS],
    }


def risk_sensitive_keys() -> set[str]:
    return {p.key for p in PARAMS if p.risk_sensitive}


@dataclass
class ParamDiff:
    changed: dict[str, tuple[Any, Any]] = field(default_factory=dict)


def diff(old: dict[str, Any], new: dict[str, Any]) -> ParamDiff:
    d = ParamDiff()
    for k, v in new.items():
        if old.get(k) != v:
            d.changed[k] = (old.get(k), v)
    return d
