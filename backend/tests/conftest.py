from __future__ import annotations

import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("AUTH_SECRET", "test-secret-" + "x" * 40)
os.environ.setdefault("COOKIE_SECURE", "false")
os.environ.setdefault("ALLOWED_ORIGINS", "http://testserver")

import pytest

from app.core.store import Store
from app.db import models as M
from app.db.session import dispose, init_engine, session_factory


@pytest.fixture
async def store(tmp_path):
    url = os.environ.get("TEST_DATABASE_URL") or f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    eng = init_engine(url)
    async with eng.begin() as conn:
        await conn.run_sync(M.Base.metadata.drop_all)
        await conn.run_sync(M.Base.metadata.create_all)
    yield Store(session_factory())
    await dispose()
