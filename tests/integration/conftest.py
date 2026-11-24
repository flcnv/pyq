import asyncio
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from uuid import uuid4

import pytest
from alembic import command
from alembic.config import Config
from redis.asyncio import Redis
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.adapters.extraction_failure_cache import RedisExtractionFailureCache
from src.adapters.persistence.database import create_session_factory
from src.adapters.rate_limiter import RedisRateLimiter
from src.adapters.redis_client import create_redis_client
from src.config import get_settings

ALEMBIC_INI = Path(__file__).parents[2] / "alembic.ini"


def alembic_config(url: URL) -> Config:
    config = Config(ALEMBIC_INI)
    rendered = url.render_as_string(hide_password=False).replace("%", "%%")
    config.set_main_option("sqlalchemy.url", rendered)
    return config


async def _recreate_database(name: str) -> None:
    # CREATE/DROP DATABASE can't run in a transaction, hence AUTOCOMMIT.
    admin = create_async_engine(
        get_settings().database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            await connection.execute(text(f'CREATE DATABASE "{name}"'))
    finally:
        await admin.dispose()


@pytest.fixture(scope="session")
def database_url() -> URL:
    """A separate `<db>_test` database, recreated per run and migrated with the real migrations."""
    settings = get_settings()
    url = settings.database_url.set(database=f"{settings.postgres_db}_test")
    assert url.database is not None
    asyncio.run(_recreate_database(url.database))
    command.upgrade(alembic_config(url), "head")
    return url


@pytest.fixture
async def session_factory(database_url: URL) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    # NullPool + one engine per test: asyncpg connections can't be shared across event loops.
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE invoices, ingest_jobs, documents"))
    yield create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    settings = get_settings()
    client = create_redis_client(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        username=settings.redis_user or None,
        password=settings.redis_password.get_secret_value() if settings.redis_password else None,
    )
    yield client
    await client.aclose()


@pytest.fixture
async def rate_limiter(redis: Redis) -> AsyncIterator[RedisRateLimiter]:
    """A key prefix per test: tests never share counters or touch other keys."""
    prefix = f"test-ratelimit-{uuid4().hex}"
    yield RedisRateLimiter(redis, key_prefix=prefix)
    keys = [key async for key in redis.scan_iter(match=f"{prefix}:*")]
    if keys:
        await redis.delete(*keys)


FAILURE_CACHE_TTL_SECONDS = 600

type CacheFactory = Callable[..., RedisExtractionFailureCache]


@pytest.fixture
async def make_cache(redis: Redis) -> AsyncIterator[CacheFactory]:
    """Caches under builds unique to the test; their keys are deleted afterwards."""
    prefix = f"test{uuid4().hex}"

    def make(config: str = "a", build: str = "b1") -> RedisExtractionFailureCache:
        """`config` and `build` stand for one extraction config and one build (git SHA)."""
        return RedisExtractionFailureCache(
            redis,
            git_sha=f"{prefix}-{build}",
            fingerprint=config,
            ttl_seconds=FAILURE_CACHE_TTL_SECONDS,
        )

    yield make
    keys = [key async for key in redis.scan_iter(match=f"llm-failure:{prefix}-*")]
    if keys:
        await redis.delete(*keys)


@pytest.fixture
async def unreachable_redis() -> AsyncIterator[Redis]:
    """The production client settings, pointed at a port where nothing listens."""
    client = create_redis_client(host="127.0.0.1", port=1)
    yield client
    await client.aclose()
