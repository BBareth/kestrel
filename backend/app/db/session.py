from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

_engine: AsyncEngine | None = None
_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(url: str, **kwargs) -> AsyncEngine:
    global _engine, _factory
    if url.startswith("sqlite"):
        eng = create_async_engine(url, **kwargs)

        @event.listens_for(eng.sync_engine, "connect")
        def _fk(dbapi_conn, _):  # noqa: ANN001
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()
    else:
        kwargs.setdefault("pool_size", 5)
        kwargs.setdefault("max_overflow", 5)
        kwargs.setdefault("pool_pre_ping", True)
        kwargs.setdefault("pool_recycle", 1800)
        eng = create_async_engine(url, **kwargs)
    _engine = eng
    _factory = async_sessionmaker(eng, expire_on_commit=False)
    return eng


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("database engine not initialised")
    return _engine


def session_factory() -> async_sessionmaker[AsyncSession]:
    if _factory is None:
        raise RuntimeError("database engine not initialised")
    return _factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional scope: commit on success, rollback on error."""
    async with session_factory()() as s:
        try:
            yield s
            await s.commit()
        except BaseException:
            await s.rollback()
            raise


async def ping() -> bool:
    try:
        async with get_engine().connect() as c:
            await c.execute(text("SELECT 1"))
        return True
    except Exception:
        return False


async def dispose() -> None:
    global _engine, _factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _factory = None
