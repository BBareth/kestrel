from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MarketExtras:
    """Non-candle context: derivatives data and microstructure."""

    funding_rate: float | None = None  # fraction per funding interval (0.0001 = 0.01%)
    next_funding_ms: int | None = None
    open_interest: float | None = None
    oi_change_1h_pct: float | None = None
    spread_bps: float | None = None
    mark_price: float | None = None
    liquidations_1h: dict[str, float] | None = None


@dataclass
class Setup:
    direction: str  # LONG | SHORT
    entry: float
    stop: float
    tp1: float
    tp2: float
    rr: float  # blended reward:risk of the TP plan
    rr_tp2: float
    confidence: float
    level: float
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    breakout_bar: int = 0
    tp1_fraction: float = 0.5

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class Evaluation:
    bar_time: int
    price: float
    decision: str  # LONG | SHORT | NO_TRADE
    setup: Setup | None
    long_checks: list[Check]
    short_checks: list[Check]
    summary: str
    regime: str
    timeframes: dict[str, Any]
    levels: list[dict[str, Any]]
    strategy: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar_time": self.bar_time,
            "price": self.price,
            "decision": self.decision,
            "setup": self.setup.to_dict() if self.setup else None,
            "long_checks": [c.to_dict() for c in self.long_checks],
            "short_checks": [c.to_dict() for c in self.short_checks],
            "summary": self.summary,
            "regime": self.regime,
            "timeframes": self.timeframes,
            "levels": self.levels,
            "strategy": self.strategy,
        }


@dataclass
class StrategyState:
    """Cross-evaluation memory (signal cooldowns). Owned by the engine / backtester."""

    last_signal_bar: dict[str, int] = field(default_factory=dict)  # direction → 5m bar open_time
