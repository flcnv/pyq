import time

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from src.adapters.persistence.database import READINESS_TIMEOUT_SECONDS, database_readiness
from src.application.errors import StorageUnavailableError
from src.config import Settings
from src.main import create_app
from tests.unit.context import app_with, make_context


async def test_health_returns_ok_and_the_build_version() -> None:
    app = app_with(create_app(), make_context(settings=Settings(git_sha="abc1234")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "abc1234"}


async def test_readiness_passes_against_the_real_database(database_url: URL) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    try:
        await database_readiness(engine)()
    finally:
        await engine.dispose()


async def test_readiness_fails_fast_when_the_database_is_unreachable(database_url: URL) -> None:
    # 192.0.2.1 (TEST-NET-1) is never routed: connecting hangs instead of being refused.
    engine = create_async_engine(database_url.set(host="192.0.2.1"), poolclass=NullPool)
    started = time.monotonic()
    try:
        with pytest.raises(StorageUnavailableError):
            await database_readiness(engine)()
    finally:
        await engine.dispose()

    assert time.monotonic() - started < READINESS_TIMEOUT_SECONDS + 1
