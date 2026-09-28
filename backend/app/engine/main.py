"""Engine process entrypoint: ``python -m app.engine.main``."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys

from sqlalchemy import text

from app.ai.provider import DisabledProvider, OpenAIProvider
from app.ai.service import AIService
from app.config import get_settings
from app.core.store import Store
from app.db.session import get_engine, init_engine, session_factory
from app.engine.runner import Engine
from app.exchange.binance_rest import BinancePublic
from app.exchange.credentials import make_live_adapter
from app.logging_setup import setup_logging
from app.market.data_service import MarketDataService
from app.notifications.service import NotificationService, PushSender

log = logging.getLogger("kestrel.engine")
ENGINE_LOCK_KEY = 7_072_026  # pg advisory lock id: only one engine may trade


async def _acquire_lock():  # noqa: ANN202
    """Hold a session-level advisory lock for the life of the process."""
    eng = get_engine()
    if eng.dialect.name != "postgresql":
        return None
    conn = await eng.connect()
    while True:
        got = (await conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": ENGINE_LOCK_KEY})).scalar()
        if got:
            return conn
        log.error("another engine instance holds the trading lock — waiting (never trade twice)")
        await asyncio.sleep(10)


async def run() -> int:
    settings = get_settings()
    setup_logging(settings.log_level, settings.secret_values(), "engine")
    problems = settings.validate_for_production()
    if problems and settings.environment == "production":
        for p in problems:
            log.critical(p)
        return 2
    init_engine(settings.database_url)
    lock_conn = await _acquire_lock()
    store = Store(session_factory())
    public = BinancePublic(settings.binance_rest_url)
    market = MarketDataService(settings.symbol, public, settings.binance_ws_market_url,
                               settings.binance_ws_public_url, session_factory())
    provider = (OpenAIProvider(settings.openai_api_key.get_secret_value(), settings.openai_base_url)
                if settings.openai_configured else DisabledProvider())
    notifier = NotificationService(store, PushSender(settings))
    engine = Engine(settings, store, notifier, market, public, AIService(provider, store),
                    make_live_adapter(settings))
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows dev
            pass
    backoff = 5
    while not stop.is_set():
        try:
            await engine.start()
            break
        except Exception:  # noqa: BLE001 - e.g. Binance unreachable at boot: retry, never crash-loop hard
            log.exception("engine start failed; retrying in %ss", backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 120)
    await stop.wait()
    log.info("engine stopping")
    await engine.stop()
    await store.system_event("info", "engine", "stopped", "engine stopped")
    if lock_conn is not None:
        await lock_conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run()))
