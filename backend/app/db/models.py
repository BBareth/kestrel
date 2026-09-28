"""Database schema.

Conventions: integer surrogate keys, UTC timestamps, floats for market values
(the exchange is the source of truth for balances; values are rounded to the
symbol's tick/step size with Decimal at the order boundary), JSON for
structured context that is displayed/audited but not queried relationally.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.db.types import JSONType, UTCDateTime, utcnow

# BIGINT on Postgres, INTEGER (rowid alias → autoincrement) on SQLite.
BigID = BigInteger().with_variant(Integer(), "sqlite")


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONType, list[Any]: JSONType, datetime: UTCDateTime}


# ---------------------------------------------------------------------------
# Users / auth
# ---------------------------------------------------------------------------
class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret_enc: Mapped[str | None] = mapped_column(String(512), default=None)
    totp_pending_enc: Mapped[str | None] = mapped_column(String(512), default=None)
    totp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    recovery_codes: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    password_changed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime)
    reauth_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


# ---------------------------------------------------------------------------
# Settings / config
# ---------------------------------------------------------------------------
class Setting(Base):
    """Key/value runtime settings (trading state, safety, notification prefs...)."""

    __tablename__ = "settings"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    updated_by: Mapped[str | None] = mapped_column(String(64), default=None)


class StrategyConfig(Base):
    """Versioned strategy + risk + AI configuration. Edits create a new version."""

    __tablename__ = "strategy_configs"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, unique=True)
    strategy_name: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSONType)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    note: Mapped[str | None] = mapped_column(String(300), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    created_by: Mapped[str | None] = mapped_column(String(64), default=None)


class ExchangeConnection(Base):
    """Credential *status* only. API keys/secrets are never stored in the database."""

    __tablename__ = "exchange_connections"
    id: Mapped[int] = mapped_column(primary_key=True)
    venue: Mapped[str] = mapped_column(String(32))
    environment: Mapped[str] = mapped_column(String(16))  # mainnet | testnet
    key_hint: Mapped[str | None] = mapped_column(String(16), default=None)  # "…ab12"
    status: Mapped[str] = mapped_column(String(16), default="missing")  # missing|invalid|connected|unsafe|error
    permissions: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    problems: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    last_checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    last_error: Mapped[str | None] = mapped_column(Text, default=None)
    __table_args__ = (UniqueConstraint("venue", "environment"),)


# ---------------------------------------------------------------------------
# Market data
# ---------------------------------------------------------------------------
class Candle(Base):
    __tablename__ = "candles"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    interval: Mapped[str] = mapped_column(String(4), primary_key=True)
    open_time: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # ms epoch
    open: Mapped[float] = mapped_column(Float)
    high: Mapped[float] = mapped_column(Float)
    low: Mapped[float] = mapped_column(Float)
    close: Mapped[float] = mapped_column(Float)
    volume: Mapped[float] = mapped_column(Float)
    quote_volume: Mapped[float] = mapped_column(Float, default=0.0)
    trades: Mapped[int] = mapped_column(Integer, default=0)
    taker_buy_volume: Mapped[float] = mapped_column(Float, default=0.0)


class FundingRate(Base):
    __tablename__ = "funding_rates"
    symbol: Mapped[str] = mapped_column(String(20), primary_key=True)
    funding_time: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rate: Mapped[float] = mapped_column(Float)


class MarketSnapshot(Base):
    __tablename__ = "market_snapshots"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    price: Mapped[float] = mapped_column(Float)
    mark_price: Mapped[float | None] = mapped_column(Float, default=None)
    funding_rate: Mapped[float | None] = mapped_column(Float, default=None)
    open_interest: Mapped[float | None] = mapped_column(Float, default=None)
    change_24h_pct: Mapped[float | None] = mapped_column(Float, default=None)
    volume_24h: Mapped[float | None] = mapped_column(Float, default=None)
    regime: Mapped[str | None] = mapped_column(String(32), default=None)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


# ---------------------------------------------------------------------------
# Signals / AI
# ---------------------------------------------------------------------------
class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    bar_time: Mapped[int] = mapped_column(BigInteger)  # 5m candle open time evaluated
    mode: Mapped[str] = mapped_column(String(8))  # paper | live
    strategy_config_id: Mapped[int | None] = mapped_column(ForeignKey("strategy_configs.id"), default=None)
    strategy_version: Mapped[int | None] = mapped_column(Integer, default=None)
    direction: Mapped[str] = mapped_column(String(8))  # LONG | SHORT
    status: Mapped[str] = mapped_column(String(24), index=True)
    # candidate → rejected_risk | rejected_ai | ai_unavailable | approved → executed | execution_failed
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    entry: Mapped[float | None] = mapped_column(Float, default=None)
    stop: Mapped[float | None] = mapped_column(Float, default=None)
    tp1: Mapped[float | None] = mapped_column(Float, default=None)
    tp2: Mapped[float | None] = mapped_column(Float, default=None)
    rr: Mapped[float | None] = mapped_column(Float, default=None)
    reasons: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    context: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    risk_result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    ai_analysis_id: Mapped[int | None] = mapped_column(ForeignKey("ai_analyses.id"), default=None)
    trade_id: Mapped[int | None] = mapped_column(BigID, default=None)
    rejection: Mapped[str | None] = mapped_column(Text, default=None)


class AIAnalysis(Base):
    __tablename__ = "ai_analyses"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    trigger: Mapped[str] = mapped_column(String(24))  # setup|regime_change|position_review|periodic|manual
    provider: Mapped[str] = mapped_column(String(24))
    model: Mapped[str] = mapped_column(String(64))
    cache_key: Mapped[str | None] = mapped_column(String(64), default=None, index=True)
    request: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONType, default=None)
    valid: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    decision: Mapped[str | None] = mapped_column(String(12), default=None)
    confidence: Mapped[float | None] = mapped_column(Float, default=None)
    market_regime: Mapped[str | None] = mapped_column(String(64), default=None)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    reasoning_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# Trades / orders / positions
# ---------------------------------------------------------------------------
class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    mode: Mapped[str] = mapped_column(String(8), index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str] = mapped_column(String(8))
    status: Mapped[str] = mapped_column(String(16), index=True)  # pending|open|closing|closed|cancelled|error
    signal_id: Mapped[int | None] = mapped_column(BigID, default=None)
    strategy_config_id: Mapped[int | None] = mapped_column(Integer, default=None)
    strategy_version: Mapped[int | None] = mapped_column(Integer, default=None)
    origin: Mapped[str] = mapped_column(String(16), default="strategy")  # strategy|recovered|manual
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    remaining_qty: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[int] = mapped_column(Integer, default=1)
    entry_price: Mapped[float | None] = mapped_column(Float, default=None)
    exit_price: Mapped[float | None] = mapped_column(Float, default=None)
    stop_price: Mapped[float | None] = mapped_column(Float, default=None)
    initial_stop: Mapped[float | None] = mapped_column(Float, default=None)
    tp1_price: Mapped[float | None] = mapped_column(Float, default=None)
    tp2_price: Mapped[float | None] = mapped_column(Float, default=None)
    tp1_filled: Mapped[bool] = mapped_column(Boolean, default=False)
    liquidation_price: Mapped[float | None] = mapped_column(Float, default=None)
    risk_usdt: Mapped[float | None] = mapped_column(Float, default=None)
    equity_at_entry: Mapped[float | None] = mapped_column(Float, default=None)
    opened_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None, index=True)
    closed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None, index=True)
    exit_reason: Mapped[str | None] = mapped_column(String(24), default=None)
    fees: Mapped[float] = mapped_column(Float, default=0.0)
    funding: Mapped[float] = mapped_column(Float, default=0.0)  # + received / - paid
    gross_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    realized_pnl: Mapped[float] = mapped_column(Float, default=0.0)  # net of fees and funding
    pnl_pct: Mapped[float | None] = mapped_column(Float, default=None)  # vs equity at entry
    r_multiple: Mapped[float | None] = mapped_column(Float, default=None)
    confidence: Mapped[float | None] = mapped_column(Float, default=None)
    ai_decision: Mapped[str | None] = mapped_column(String(12), default=None)
    ai_confidence: Mapped[float | None] = mapped_column(Float, default=None)
    market_regime: Mapped[str | None] = mapped_column(String(64), default=None)
    reason: Mapped[str | None] = mapped_column(Text, default=None)
    reasons: Mapped[list[Any]] = mapped_column(JSONType, default=list)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    # Current protective orders: {"sl": client_id, "tp1": client_id, "tp2": client_id, "seq": n, ...}
    protection: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class TradeEvent(Base):
    """Per-trade audit trail: signal → approvals → orders → fills → close → P&L."""

    __tablename__ = "trade_events"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    trade_id: Mapped[int] = mapped_column(BigID, ForeignKey("trades.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    step: Mapped[str] = mapped_column(String(32))
    message: Mapped[str] = mapped_column(Text, default="")
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    trade_id: Mapped[int | None] = mapped_column(BigID, ForeignKey("trades.id", ondelete="SET NULL"), index=True)
    mode: Mapped[str] = mapped_column(String(8), index=True)
    symbol: Mapped[str] = mapped_column(String(20))
    kind: Mapped[str] = mapped_column(String(16))  # entry|stop|tp1|tp2|exit|emergency|test
    client_order_id: Mapped[str] = mapped_column(String(40), unique=True)
    venue_order_id: Mapped[str | None] = mapped_column(String(40), default=None)
    conditional: Mapped[bool] = mapped_column(Boolean, default=False)  # Binance algo order
    side: Mapped[str] = mapped_column(String(4))
    type: Mapped[str] = mapped_column(String(24))
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    price: Mapped[float | None] = mapped_column(Float, default=None)
    trigger_price: Mapped[float | None] = mapped_column(Float, default=None)
    reduce_only: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(20), index=True)
    filled_qty: Mapped[float] = mapped_column(Float, default=0.0)
    avg_price: Mapped[float | None] = mapped_column(Float, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    raw: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)


class Position(Base):
    """Latest observed position per mode/symbol (the exchange is the source of truth)."""

    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(primary_key=True)
    mode: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(20))
    direction: Mapped[str | None] = mapped_column(String(8), default=None)
    quantity: Mapped[float] = mapped_column(Float, default=0.0)
    entry_price: Mapped[float | None] = mapped_column(Float, default=None)
    mark_price: Mapped[float | None] = mapped_column(Float, default=None)
    unrealized_pnl: Mapped[float] = mapped_column(Float, default=0.0)
    leverage: Mapped[int | None] = mapped_column(Integer, default=None)
    liquidation_price: Mapped[float | None] = mapped_column(Float, default=None)
    margin: Mapped[float | None] = mapped_column(Float, default=None)
    trade_id: Mapped[int | None] = mapped_column(BigID, default=None)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    __table_args__ = (UniqueConstraint("mode", "symbol"),)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    mode: Mapped[str] = mapped_column(String(8), index=True)
    equity: Mapped[float] = mapped_column(Float)
    wallet: Mapped[float] = mapped_column(Float)
    unrealized: Mapped[float] = mapped_column(Float, default=0.0)


# ---------------------------------------------------------------------------
# Events / audit / notifications
# ---------------------------------------------------------------------------
class RiskEvent(Base):
    __tablename__ = "risk_events"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(48))
    severity: Mapped[str] = mapped_column(String(12))  # info|warning|critical
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class SystemEvent(Base):
    __tablename__ = "system_events"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    level: Mapped[str] = mapped_column(String(12))
    component: Mapped[str] = mapped_column(String(32))
    event: Mapped[str] = mapped_column(String(64))
    message: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, default=None)
    username: Mapped[str | None] = mapped_column(String(64), default=None)
    action: Mapped[str] = mapped_column(String(64), index=True)
    target: Mapped[str | None] = mapped_column(String(128), default=None)
    ip: Mapped[str | None] = mapped_column(String(64), default=None)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    category: Mapped[str] = mapped_column(String(24))
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    severity: Mapped[str] = mapped_column(String(12), default="info")
    delivered: Mapped[int] = mapped_column(Integer, default=0)
    failed: Mapped[int] = mapped_column(Integer, default=0)
    suppressed: Mapped[bool] = mapped_column(Boolean, default=False)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PushSubscription(Base):
    __tablename__ = "push_subscriptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    endpoint: Mapped[str] = mapped_column(Text, unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    user_agent: Mapped[str | None] = mapped_column(String(300), default=None)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_success_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)


# ---------------------------------------------------------------------------
# Engine plumbing
# ---------------------------------------------------------------------------
class ComponentStatus(Base):
    __tablename__ = "component_status"
    component: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(12))  # ok|degraded|down|disabled|unknown
    detail: Mapped[str | None] = mapped_column(Text, default=None)
    data: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)


class LiveState(Base):
    """Small JSON blobs the engine publishes for the UI (ticker, evaluation...)."""

    __tablename__ = "live_state"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONType)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)


class Command(Base):
    """API → engine command queue (kill switch, manual close, analyze now...)."""

    __tablename__ = "commands"
    id: Mapped[int] = mapped_column(BigID, primary_key=True)
    ts: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(12), default="pending", index=True)  # pending|done|failed
    processed_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    result: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    requested_by: Mapped[str | None] = mapped_column(String(64), default=None)


class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime, default=None)
    status: Mapped[str] = mapped_column(String(12), default="queued")  # queued|running|done|failed
    params: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONType, default=None)
    error: Mapped[str | None] = mapped_column(Text, default=None)
    progress: Mapped[float] = mapped_column(Float, default=0.0)


Index("ix_orders_trade_kind", Order.trade_id, Order.kind)
Index("ix_trades_mode_status", Trade.mode, Trade.status)
