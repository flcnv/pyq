import random
from dataclasses import dataclass, field

from src.application.errors import (
    ExtractionFailedError,
    RateLimiterUnavailableError,
    StorageUnavailableError,
    UnusableLlmOutputError,
)


def is_transient(error: Exception) -> bool:
    """Only failures that may succeed later: an explicit list, not "anything unexpected".

    An unknown error is almost always a bug; retrying it wouldn't help, and once Claude has
    answered, each retry would be another paid call the failure cache doesn't catch.
    """
    if isinstance(error, UnusableLlmOutputError):
        return False  # Claude answered: the same input gets the same answer
    return isinstance(
        error,
        ExtractionFailedError  # LLM timeout, network error, 5xx, 429
        | RateLimiterUnavailableError  # the LLM budget can't be checked (Redis down)
        | StorageUnavailableError,  # the database is unreachable
    )


@dataclass(frozen=True, slots=True, kw_only=True)
class RetryPolicy:
    max_attempts: int
    base_seconds: float
    cap_seconds: float
    rng: random.Random = field(default_factory=random.Random)

    def delay_for(self, attempt: int) -> float:
        """Exponential backoff with "equal jitter": between half and all of
        min(cap, base * 2^(attempt - 1)). Never immediate, and retries of jobs that failed
        together spread out instead of hitting a recovering service at the same moment."""
        ceiling: float = min(self.cap_seconds, self.base_seconds * 2.0 ** (attempt - 1))
        return ceiling / 2 + self.rng.uniform(0, ceiling / 2)
