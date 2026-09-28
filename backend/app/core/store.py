"""Data-access layer shared by the engine and the API."""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, desc, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core import params as P
from app.db import models as M
from app.db.types import utcnow

log = logging.getLogger("kestrel.store")

TRADING_DEFAULT: dict[str, Any] = {
    "mode": "paper",
    "kill_switch": False,
    "strategy_enabled": True,
    "halted": False,
    "halt_reason": None,
    "halted_at": None,
    "live_enabled_at": None,
    "live_enabled_by": None,
    "kill_engaged_at": None,
}

NOTIFY_DEFAULT: dict[str, bool] = {
    "trade_opened": True,
    "stop_loss": True,
    "take_profit": True,
    "trade_closed": True,
    "signal": True,
    "risk": True,
    "system": True,
    "live_mode": True,
    "ai": False,
}

OPEN_STATUSES = ("pending", "open", "closing")


def day_start(now: datetime) -> datetime:
    now = now.astimezone(UTC)
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def week_start(now: datetime) -> datetime:
    d = day_start(now)
    return d - timedelta(days=d.weekday())


class Store:
    def __init__(self, factory: async_sessionmaker[AsyncSession]):
        self.factory = factory

    # ------------------------------------------------------------------ settings
    async def get_setting(self, key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
        async with self.factory() as s:
            row = await s.get(M.Setting, key)
            if row is None:
                return dict(default or {})
            return dict(row.value)

    async def set_setting(self, key: str, value: dict[str, Any], by: str | None = None) -> None:
        async with self.factory() as s:
            row = await s.get(M.Setting, key)
            if row is None:
                s.add(M.Setting(key=key, value=value, updated_by=by))
            else:
                row.value = value
                row.updated_by = by
                row.updated_at = utcnow()
            await s.commit()

    async def update_setting(self, key: str, patch: dict[str, Any], default: dict[str, Any] | None = None,
                             by: str | None = None) -> dict[str, Any]:
        async with self.factory() as s:
            row = await s.get(M.Setting, key, with_for_update=True)
            base = dict(default or {}) if row is None else dict(row.value)
            base.update(patch)
            if row is None:
                s.add(M.Setting(key=key, value=base, updated_by=by))
            else:
                row.value = base
                row.updated_by = by
                row.updated_at = utcnow()
            await s.commit()
            return base

    async def trading_state(self) -> dict[str, Any]:
        st = await self.get_setting("trading", TRADING_DEFAULT)
        return {**TRADING_DEFAULT, **st}

    async def update_trading(self, patch: dict[str, Any], by: str | None = None) -> dict[str, Any]:
        return await self.update_setting("trading", patch, TRADING_DEFAULT, by)

    async def safety(self) -> dict[str, Any]:
        clean, _ = P.validate_safety(await self.get_setting("safety", P.safety_defaults()))
        return clean

    async def notify_prefs(self) -> dict[str, bool]:
        return {**NOTIFY_DEFAULT, **await self.get_setting("notifications", NOTIFY_DEFAULT)}

    async def mark_readiness(self, item: str, ok: bool = True, detail: str | None = None) -> None:
        await self.update_setting("readiness", {item: {"at": utcnow().isoformat(), "ok": ok, "detail": detail}})

    # ------------------------------------------------------------------ strategy config
    async def active_config(self) -> M.StrategyConfig:
        async with self.factory() as s:
            row = (await s.execute(select(M.StrategyConfig).where(M.StrategyConfig.is_active.is_(True))
                                   .order_by(desc(M.StrategyConfig.version)).limit(1))).scalar_one_or_none()
            if row is not None:
                return row
        return await self.save_config(P.defaults(), by="system", note="initial safe defaults")

    async def active_params(self) -> tuple[M.StrategyConfig, dict[str, Any]]:
        cfg = await self.active_config()
        return cfg, P.enforce(cfg.params)

    async def save_config(self, params: dict[str, Any], by: str | None, note: str | None = None) -> M.StrategyConfig:
        clean, errors = P.validate(params)
        if errors:
            raise ValueError("; ".join(errors))
        async with self.factory() as s:
            maxv = (await s.execute(select(func.max(M.StrategyConfig.version)))).scalar() or 0
            await s.execute(update(M.StrategyConfig).values(is_active=False))
            row = M.StrategyConfig(version=maxv + 1, strategy_name=P.STRATEGY_NAME, params=clean, is_active=True,
                                   note=note, created_by=by)
            s.add(row)
            await s.commit()
            await s.refresh(row)
            return row

    # ------------------------------------------------------------------ events
    async def system_event(self, level: str, component: str, event: str, message: str,
                           data: dict[str, Any] | None = None) -> None:
        log.log(logging.getLevelName(level.upper()) if level.upper() in ("INFO", "WARNING", "ERROR", "CRITICAL")
                else logging.INFO, message, extra={"component": component, "event": event})
        try:
            async with self.factory() as s:
                s.add(M.SystemEvent(level=level, component=component, event=event, message=message[:4000],
                                    data=data or {}))
                await s.commit()
        except Exception:  # noqa: BLE001 - never let event logging break trading
            log.exception("failed to persist system event")

    async def risk_event(self, kind: str, severity: str, message: str, data: dict[str, Any] | None = None) -> None:
        log.warning(message, extra={"risk_event": kind, "severity": severity})
        try:
            async with self.factory() as s:
                s.add(M.RiskEvent(kind=kind, severity=severity, message=message, data=data or {}))
                await s.commit()
        except Exception:  # noqa: BLE001
            log.exception("failed to persist risk event")

    async def audit(self, action: str, user: M.User | None = None, target: str | None = None,
                    ip: str | None = None, data: dict[str, Any] | None = None, username: str | None = None) -> None:
        async with self.factory() as s:
            s.add(M.AuditLog(user_id=user.id if user else None, username=user.username if user else username,
                             action=action, target=target, ip=ip, data=data or {}))
            await s.commit()

    async def trade_event(self, trade_id: int, step: str, message: str = "", data: dict[str, Any] | None = None) -> None:
        async with self.factory() as s:
            s.add(M.TradeEvent(trade_id=trade_id, step=step, message=message, data=data or {}))
            await s.commit()

    async def set_component(self, component: str, status: str, detail: str | None = None,
                            data: dict[str, Any] | None = None) -> None:
        now = utcnow()
        async with self.factory() as s:
            row = await s.get(M.ComponentStatus, component)
            if row is None:
                row = M.ComponentStatus(component=component, status=status, detail=detail, data=data or {},
                                        updated_at=now, last_ok_at=now if status == "ok" else None)
                s.add(row)
            else:
                row.status = status
                row.detail = detail
                if data is not None:
                    row.data = data
                row.updated_at = now
                if status == "ok":
                    row.last_ok_at = now
            await s.commit()

    async def components(self) -> dict[str, M.ComponentStatus]:
        async with self.factory() as s:
            rows = (await s.execute(select(M.ComponentStatus))).scalars().all()
            return {r.component: r for r in rows}

    async def publish(self, key: str, value: dict[str, Any]) -> None:
        async with self.factory() as s:
            row = await s.get(M.LiveState, key)
            if row is None:
                s.add(M.LiveState(key=key, value=value, updated_at=utcnow()))
            else:
                row.value = value
                row.updated_at = utcnow()
            await s.commit()

    async def live_state(self, keys: list[str] | None = None) -> dict[str, dict[str, Any]]:
        async with self.factory() as s:
            q = select(M.LiveState)
            if keys:
                q = q.where(M.LiveState.key.in_(keys))
            rows = (await s.execute(q)).scalars().all()
            return {r.key: {**r.value, "_updated_at": r.updated_at.isoformat()} for r in rows}

    # ------------------------------------------------------------------ commands
    async def enqueue_command(self, kind: str, payload: dict[str, Any] | None = None, by: str | None = None) -> int:
        async with self.factory() as s:
            c = M.Command(kind=kind, payload=payload or {}, requested_by=by)
            s.add(c)
            await s.commit()
            await s.refresh(c)
            return c.id

    async def pending_commands(self) -> list[M.Command]:
        async with self.factory() as s:
            return list((await s.execute(select(M.Command).where(M.Command.status == "pending")
                                         .order_by(M.Command.id).limit(20))).scalars().all())

    async def finish_command(self, cid: int, status: str, result: dict[str, Any] | None = None) -> None:
        async with self.factory() as s:
            await s.execute(update(M.Command).where(M.Command.id == cid)
                            .values(status=status, result=result or {}, processed_at=utcnow()))
            await s.commit()

    async def get_command(self, cid: int) -> M.Command | None:
        async with self.factory() as s:
            return await s.get(M.Command, cid)

    # ------------------------------------------------------------------ trades / orders
    async def create_trade(self, **fields: Any) -> M.Trade:
        async with self.factory() as s:
            t = M.Trade(**fields)
            s.add(t)
            await s.commit()
            await s.refresh(t)
            return t

    async def update_trade(self, trade_id: int, **fields: Any) -> M.Trade:
        async with self.factory() as s:
            t = await s.get(M.Trade, trade_id)
            if t is None:
                raise KeyError(trade_id)
            for k, v in fields.items():
                setattr(t, k, v)
            await s.commit()
            await s.refresh(t)
            return t

    async def get_trade(self, trade_id: int) -> M.Trade | None:
        async with self.factory() as s:
            return await s.get(M.Trade, trade_id)

    async def open_trades(self, mode: str | None = None) -> list[M.Trade]:
        async with self.factory() as s:
            q = select(M.Trade).where(M.Trade.status.in_(OPEN_STATUSES))
            if mode:
                q = q.where(M.Trade.mode == mode)
            return list((await s.execute(q.order_by(M.Trade.id))).scalars().all())

    async def create_order(self, **fields: Any) -> M.Order:
        async with self.factory() as s:
            o = M.Order(**fields)
            s.add(o)
            await s.commit()
            await s.refresh(o)
            return o

    async def update_order(self, client_order_id: str, **fields: Any) -> None:
        async with self.factory() as s:
            await s.execute(update(M.Order).where(M.Order.client_order_id == client_order_id)
                            .values(**fields, updated_at=utcnow()))
            await s.commit()

    async def trade_orders(self, trade_id: int) -> list[M.Order]:
        async with self.factory() as s:
            return list((await s.execute(select(M.Order).where(M.Order.trade_id == trade_id)
                                         .order_by(M.Order.id))).scalars().all())

    async def order_by_client_id(self, client_order_id: str) -> M.Order | None:
        async with self.factory() as s:
            return (await s.execute(select(M.Order).where(M.Order.client_order_id == client_order_id))).scalar_one_or_none()

    async def active_orders(self, mode: str) -> list[M.Order]:
        async with self.factory() as s:
            return list((await s.execute(select(M.Order).where(
                M.Order.mode == mode, M.Order.status.in_(("NEW", "PARTIALLY_FILLED", "SUBMITTING"))))).scalars().all())

    async def upsert_position(self, mode: str, symbol: str, **fields: Any) -> None:
        async with self.factory() as s:
            row = (await s.execute(select(M.Position).where(M.Position.mode == mode, M.Position.symbol == symbol))
                   ).scalar_one_or_none()
            if row is None:
                s.add(M.Position(mode=mode, symbol=symbol, **fields))
            else:
                for k, v in fields.items():
                    setattr(row, k, v)
                row.updated_at = utcnow()
            await s.commit()

    async def trade_stats(self, mode: str, now: datetime, equity: float) -> dict[str, Any]:
        """Inputs for the risk engine: realised P&L windows, streaks, frequencies."""
        ds, ws = day_start(now), week_start(now)
        async with self.factory() as s:
            closed_week = (await s.execute(select(M.Trade).where(
                M.Trade.mode == mode, M.Trade.status == "closed", M.Trade.closed_at >= ws))).scalars().all()
            realized_today = sum(t.realized_pnl for t in closed_week if t.closed_at and t.closed_at >= ds)
            realized_week = sum(t.realized_pnl for t in closed_week)
            recent = (await s.execute(select(M.Trade).where(M.Trade.mode == mode, M.Trade.status == "closed")
                                      .order_by(desc(M.Trade.closed_at)).limit(50))).scalars().all()
            streak = 0
            last_loss = None
            for t in recent:
                if t.realized_pnl < 0:
                    streak += 1
                    last_loss = last_loss or t.closed_at
                else:
                    break
            if last_loss is None:
                last_any_loss = next((t for t in recent if t.realized_pnl < 0), None)
                last_loss = last_any_loss.closed_at if last_any_loss else None
            opened_hour = (await s.execute(select(func.count()).select_from(M.Trade).where(
                M.Trade.mode == mode, M.Trade.opened_at >= now - timedelta(hours=1),
                M.Trade.status.notin_(("cancelled",))))).scalar() or 0
            opened_today = (await s.execute(select(func.count()).select_from(M.Trade).where(
                M.Trade.mode == mode, M.Trade.opened_at >= ds, M.Trade.status.notin_(("cancelled",))))).scalar() or 0
            open_count = (await s.execute(select(func.count()).select_from(M.Trade).where(
                M.Trade.mode == mode, M.Trade.status.in_(OPEN_STATUSES)))).scalar() or 0
        marks = await self.get_setting("equity_marks", {})
        m = marks.get(mode, {})
        day_key, week_key = ds.date().isoformat(), ws.date().isoformat()
        changed = False
        if m.get("day") != day_key:
            m["day"], m["day_start"] = day_key, equity - 0.0
            changed = True
        if m.get("week") != week_key:
            m["week"], m["week_start"] = week_key, equity
            changed = True
        if changed:
            marks[mode] = m
            await self.set_setting("equity_marks", marks)
        return {
            "day_start_equity": float(m.get("day_start") or equity),
            "week_start_equity": float(m.get("week_start") or equity),
            "realized_today": realized_today,
            "realized_week": realized_week,
            "consecutive_losses": streak,
            "last_loss_at": last_loss,
            "trades_last_hour": int(opened_hour),
            "trades_today": int(opened_today),
            "open_positions": int(open_count),
            "has_open_trade": open_count > 0,
        }

    async def record_equity(self, mode: str, equity: float, wallet: float, unrealized: float) -> None:
        async with self.factory() as s:
            s.add(M.EquitySnapshot(mode=mode, equity=equity, wallet=wallet, unrealized=unrealized))
            await s.commit()

    # ------------------------------------------------------------------ signals
    async def create_signal(self, **fields: Any) -> M.Signal:
        async with self.factory() as s:
            sig = M.Signal(**fields)
            s.add(sig)
            await s.commit()
            await s.refresh(sig)
            return sig

    async def update_signal(self, sid: int, **fields: Any) -> None:
        async with self.factory() as s:
            await s.execute(update(M.Signal).where(M.Signal.id == sid).values(**fields))
            await s.commit()

    # ------------------------------------------------------------------ AI
    async def record_analysis(self, **fields: Any) -> M.AIAnalysis:
        async with self.factory() as s:
            a = M.AIAnalysis(**fields)
            s.add(a)
            await s.commit()
            await s.refresh(a)
            return a

    async def ai_spend_since(self, since: datetime) -> tuple[float, int]:
        async with self.factory() as s:
            row = (await s.execute(select(func.coalesce(func.sum(M.AIAnalysis.cost_usd), 0.0), func.count())
                                   .where(M.AIAnalysis.ts >= since))).one()
            return float(row[0] or 0.0), int(row[1] or 0)

    async def cached_analysis(self, cache_key: str, max_age: timedelta) -> M.AIAnalysis | None:
        async with self.factory() as s:
            return (await s.execute(select(M.AIAnalysis).where(
                M.AIAnalysis.cache_key == cache_key, M.AIAnalysis.valid.is_(True),
                M.AIAnalysis.ts >= utcnow() - max_age).order_by(desc(M.AIAnalysis.id)).limit(1))).scalar_one_or_none()

    async def last_analysis(self, trigger: str | None = None) -> M.AIAnalysis | None:
        async with self.factory() as s:
            q = select(M.AIAnalysis)
            if trigger:
                q = q.where(M.AIAnalysis.trigger == trigger)
            return (await s.execute(q.order_by(desc(M.AIAnalysis.id)).limit(1))).scalar_one_or_none()

    # ------------------------------------------------------------------ misc queries
    async def recent_closed_trades(self, mode: str, limit: int = 5) -> list[M.Trade]:
        async with self.factory() as s:
            return list((await s.execute(select(M.Trade).where(M.Trade.mode == mode, M.Trade.status == "closed")
                                         .order_by(desc(M.Trade.closed_at)).limit(limit))).scalars().all())

    async def unresolved_critical(self, since: datetime) -> int:
        async with self.factory() as s:
            return int((await s.execute(select(func.count()).select_from(M.SystemEvent).where(
                M.SystemEvent.level == "critical", M.SystemEvent.resolved.is_(False),
                M.SystemEvent.ts >= since))).scalar() or 0)

    async def resolve_critical(self) -> None:
        async with self.factory() as s:
            await s.execute(update(M.SystemEvent).where(M.SystemEvent.level == "critical")
                            .values(resolved=True))
            await s.commit()

    async def prune(self, now: datetime) -> None:
        """Retention: keep candles, trades, signals with trades, audit logs forever; trim chatty tables."""
        async with self.factory() as s:
            await s.execute(delete(M.MarketSnapshot).where(M.MarketSnapshot.ts < now - timedelta(days=90)))
            await s.execute(delete(M.EquitySnapshot).where(M.EquitySnapshot.ts < now - timedelta(days=365)))
            await s.execute(delete(M.Command).where(M.Command.ts < now - timedelta(days=30),
                                                    M.Command.status != "pending"))
            await s.execute(delete(M.Session).where(or_(M.Session.expires_at < now, M.Session.revoked.is_(True)),
                                                    M.Session.last_seen_at < now - timedelta(days=7)))
            await s.execute(delete(M.Signal).where(and_(M.Signal.ts < now - timedelta(days=180),
                                                        M.Signal.trade_id.is_(None))))
            await s.commit()
