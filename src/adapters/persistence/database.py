from sqlalchemy import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


def create_engine(url: URL) -> AsyncEngine:
    # pre_ping: survive Postgres restarts without failing the first request on a stale connection.
    # hide_parameters: SQL errors would otherwise carry the row's values (full IBANs, names) into
    # exception messages, and from there into the logs.
    return create_async_engine(url, pool_pre_ping=True, hide_parameters=True)


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    # expire_on_commit=False: records stay readable after commit, for mapping back to the domain.
    return async_sessionmaker(engine, expire_on_commit=False)
