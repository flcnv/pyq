import logging

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.application.errors import RateLimiterUnavailableError, RateLimitExceededError
from src.application.ports.rate_limiter import RateLimit

logger = logging.getLogger(__name__)

KEY_PREFIX = "ratelimit"


class RedisRateLimiter:
    """Fixed-window counter: INCR a key that expires when its window ends.

    The window starts at the first request, so no clock is shared between app instances. A fixed
    window can let up to 2x the limit through around a window boundary: acceptable for cost
    protection, and one key per client instead of a log of timestamps.
    """

    def __init__(self, redis: Redis, *, key_prefix: str = KEY_PREFIX) -> None:
        self._redis = redis
        self._key_prefix = key_prefix

    async def hit(self, rate_limit: RateLimit, key: str) -> None:
        redis_key = f"{self._key_prefix}:{rate_limit.name}:{key}"
        try:
            async with self._redis.pipeline(transaction=True) as pipeline:
                pipeline.incr(redis_key)
                pipeline.expire(redis_key, rate_limit.window_seconds, nx=True)
                pipeline.ttl(redis_key)
                count, _, ttl = await pipeline.execute()
        except RedisError as error:
            logger.warning("rate limiter unavailable", extra={"error": type(error).__name__})
            raise RateLimiterUnavailableError("Rate limits can't be checked right now") from error

        if count > rate_limit.limit:
            raise RateLimitExceededError(
                "Too many requests, try again later",
                retry_after_seconds=max(ttl, 1),
            )
