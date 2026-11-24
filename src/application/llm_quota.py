from src.application.errors import LlmQuotaExceededError, RateLimitExceededError
from src.application.ports.rate_limiter import RateLimit, RateLimiter

HOUR = 60 * 60
DAY = 24 * HOUR


class LlmQuota:
    """Caps LLM calls per client and in total, so a public endpoint can't drain the API key.

    Fails closed: if the limiter is unavailable, `RateLimiterUnavailableError` propagates and
    the LLM is not called.
    """

    def __init__(self, limiter: RateLimiter, *, per_client_per_hour: int, per_day: int) -> None:
        self._limiter = limiter
        self._per_client = RateLimit(
            name="llm_client", limit=per_client_per_hour, window_seconds=HOUR
        )
        self._global = RateLimit(name="llm_global", limit=per_day, window_seconds=DAY)

    async def consume(self, client_id: str) -> None:
        try:
            await self._limiter.hit(self._per_client, client_id)
            await self._limiter.hit(self._global, "all")
        except RateLimitExceededError as error:
            raise LlmQuotaExceededError(
                "LLM extraction quota exceeded, try again later",
                retry_after_seconds=error.retry_after_seconds,
            ) from error
