from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True, kw_only=True)
class RateLimit:
    name: str
    limit: int
    window_seconds: int


class RateLimiter(Protocol):
    async def hit(self, rate_limit: RateLimit, key: str) -> None:
        """Count one request for `key`.

        Raises `RateLimitExceededError` when the limit is exceeded within the window, and
        `RateLimiterUnavailableError` when the store can't be reached.
        """
        ...
