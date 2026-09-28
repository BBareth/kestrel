"""API process: ``uvicorn app.api.main:app``."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app import __version__
from app.api.deps import AppContext
from app.api.routes import auth, config, data, system, trading
from app.api.security import RateLimiter
from app.config import Settings, get_settings
from app.core.store import Store
from app.db.session import dispose, init_engine, session_factory
from app.exchange.binance_rest import BinancePublic
from app.exchange.credentials import make_live_adapter
from app.logging_setup import setup_logging
from app.notifications.service import NotificationService, PushSender

log = logging.getLogger("kestrel.api")
MAX_BODY = 256 * 1024


def create_app(settings: Settings | None = None, init_db: bool = True) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # noqa: ANN202
        setup_logging(settings.log_level, settings.secret_values(), "api")
        problems = settings.validate_for_production()
        if problems and settings.environment == "production":
            raise RuntimeError("; ".join(problems))
        if init_db:
            init_engine(settings.database_url)
        store = Store(session_factory())
        public = BinancePublic(settings.binance_rest_url)
        app.state.ctx = AppContext(settings=settings, store=store, limiter=RateLimiter(),
                                   notifier=NotificationService(store, PushSender(settings)),
                                   live_adapter=make_live_adapter(settings), public=public)
        log.info("api started", extra={"version": __version__})
        yield
        await public.close()
        if init_db:
            await dispose()

    app = FastAPI(title="Kestrel", version=__version__, lifespan=lifespan,
                  docs_url=None if settings.environment == "production" else "/api/docs",
                  redoc_url=None, openapi_url=None if settings.environment == "production" else "/api/openapi.json")

    @app.middleware("http")
    async def guard(request: Request, call_next):  # noqa: ANN001, ANN202
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > MAX_BODY:
            return JSONResponse({"detail": "request too large"}, status_code=413)
        resp = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "same-origin"
        return resp

    @app.exception_handler(RequestValidationError)
    async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        errs = [f"{'.'.join(str(x) for x in e.get('loc', [])[1:])}: {e.get('msg')}" for e in exc.errors()[:5]]
        return JSONResponse({"detail": {"message": "invalid request", "errors": errs}}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled API error", extra={"path": request.url.path})
        return JSONResponse({"detail": "internal error"}, status_code=500)

    for r in (auth.router, trading.router, data.router, config.router, system.router):
        app.include_router(r)
    return app


app = create_app()
