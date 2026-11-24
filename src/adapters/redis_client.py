from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff

TIMEOUT_SECONDS = 0.5


def create_redis_client(
    *,
    host: str,
    port: int,
    db: int = 0,
    username: str | None = None,
    password: str | None = None,
) -> Redis:
    """Short timeouts and no retries: when Redis is down, every caller degrades within 0.5 s.

    redis-py retries with backoff by default, which turned one outage into seconds of delay on
    every upload (limit check, cache read, cache write) before failing open.
    """
    return Redis(
        host=host,
        port=port,
        db=db,
        username=username,
        password=password,
        socket_timeout=TIMEOUT_SECONDS,
        socket_connect_timeout=TIMEOUT_SECONDS,
        retry=Retry(NoBackoff(), retries=0),
    )
