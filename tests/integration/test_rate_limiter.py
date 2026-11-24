import asyncio

import pytest
from redis.asyncio import Redis

from src.adapters.rate_limiter import RedisRateLimiter
from src.application.errors import RateLimiterUnavailableError, RateLimitExceededError
from src.application.ports.rate_limiter import RateLimit

LIMIT = RateLimit(name="uploads", limit=3, window_seconds=60)


async def test_allows_requests_up_to_the_limit(rate_limiter: RedisRateLimiter) -> None:
    for _ in range(LIMIT.limit):
        await rate_limiter.hit(LIMIT, "client-a")


async def test_rejects_the_request_over_the_limit_with_retry_after(
    rate_limiter: RedisRateLimiter,
) -> None:
    for _ in range(LIMIT.limit):
        await rate_limiter.hit(LIMIT, "client-a")

    with pytest.raises(RateLimitExceededError) as raised:
        await rate_limiter.hit(LIMIT, "client-a")

    assert 1 <= raised.value.retry_after_seconds <= LIMIT.window_seconds


async def test_counts_each_client_and_each_limit_separately(
    rate_limiter: RedisRateLimiter,
) -> None:
    other_limit = RateLimit(name="llm_client", limit=LIMIT.limit, window_seconds=60)
    for _ in range(LIMIT.limit):
        await rate_limiter.hit(LIMIT, "client-a")

    await rate_limiter.hit(LIMIT, "client-b")
    await rate_limiter.hit(other_limit, "client-a")


async def test_window_resets_after_it_expires(rate_limiter: RedisRateLimiter, redis: Redis) -> None:
    short = RateLimit(name="short", limit=1, window_seconds=60)
    await rate_limiter.hit(short, "client-a")
    with pytest.raises(RateLimitExceededError):
        await rate_limiter.hit(short, "client-a")

    key = await _only_key(redis, "*:short:client-a")
    await redis.pexpire(key, 10)  # fast-forward to the end of the window
    await _wait_until_expired(redis, key)

    await rate_limiter.hit(short, "client-a")


async def test_window_is_not_extended_by_later_requests(
    rate_limiter: RedisRateLimiter, redis: Redis
) -> None:
    long = RateLimit(name="long", limit=10, window_seconds=600)
    await rate_limiter.hit(long, "client-a")
    key = await _only_key(redis, "*:long:client-a")
    await redis.expire(key, 5)

    await rate_limiter.hit(long, "client-a")

    assert await redis.ttl(key) <= 5


async def test_unreachable_redis_is_reported_as_unavailable(unreachable_redis: Redis) -> None:
    limiter = RedisRateLimiter(unreachable_redis)

    with pytest.raises(RateLimiterUnavailableError):
        await limiter.hit(LIMIT, "client-a")


async def _only_key(redis: Redis, pattern: str) -> str:
    [key] = [key async for key in redis.scan_iter(match=pattern)]
    return key.decode()


async def _wait_until_expired(redis: Redis, key: str) -> None:
    for _ in range(100):
        if not await redis.exists(key):
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{key} did not expire")
