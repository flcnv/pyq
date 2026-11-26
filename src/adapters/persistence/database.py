import asyncio

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.application.errors import StorageUnavailableError
from src.application.ports.readiness import ReadinessCheck

# A load balancer polls this: answer quickly, instead of waiting for the driver's connect timeout.
READINESS_TIMEOUT_SECONDS = 2.0


def create_engine(url: URL) -> AsyncEngine:
    # pre_ping: survive Postgres restarts without failing the first request on a stale connection.
    # hide_parameters: SQL errors would otherwise carry the row's values (full IBANs, names) into
    # exception messages, and from there into the logs.
    return create_async_engine(url, pool_pre_ping=True, hide_parameters=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: records stay readable after commit, for mapping back to the domain.
    return async_sessionmaker(engine, expire_on_commit=False)


def database_readiness(engine: AsyncEngine) -> ReadinessCheck:
    async def check() -> None:
        try:
            async with asyncio.timeout(READINESS_TIMEOUT_SECONDS), engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception as error:  # whatever the cause, the database can't serve requests now
            raise StorageUnavailableError("The database is unavailable") from error

    return check
