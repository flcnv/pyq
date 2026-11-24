import random

import pytest

from src.application.errors import (
    ExtractionFailedError,
    LlmFallbackNotConfiguredError,
    LlmQuotaExceededError,
    RateLimiterUnavailableError,
    StorageUnavailableError,
    UnreadableDocumentError,
    UnusableLlmOutputError,
)
from src.application.retry_policy import RetryPolicy, is_transient
from src.domain.errors import InvalidInvoiceError, InvalidQrPayloadError

POLICY = RetryPolicy(max_attempts=5, base_seconds=2, cap_seconds=60, rng=random.Random(42))


@pytest.mark.parametrize(
    ("error", "transient"),
    [
        (ExtractionFailedError("timeout, network, 5xx, 429"), True),
        (RateLimiterUnavailableError("redis down"), True),
        (StorageUnavailableError("database down"), True),
        (UnusableLlmOutputError("Claude answered, but in prose"), False),
        (InvalidInvoiceError(()), False),
        (InvalidQrPayloadError(()), False),
        (UnreadableDocumentError("corrupt"), False),
        (LlmQuotaExceededError("over budget", retry_after_seconds=60), False),
        (LlmFallbackNotConfiguredError("no key"), False),
        (RuntimeError("a bug"), False),
        (KeyError("a bug"), False),
    ],
    ids=lambda value: type(value).__name__ if isinstance(value, Exception) else str(value),
)
def test_only_the_explicit_list_is_transient(error: Exception, transient: bool) -> None:
    assert is_transient(error) is transient


@pytest.mark.parametrize("attempt", range(1, 11))
def test_delay_is_between_half_and_all_of_the_capped_exponential(attempt: int) -> None:
    ceiling = min(60, 2 * 2 ** (attempt - 1))

    for _ in range(50):
        delay = POLICY.delay_for(attempt)
        assert ceiling / 2 <= delay <= ceiling


def test_delay_grows_then_stays_at_the_cap() -> None:
    no_jitter = RetryPolicy(max_attempts=10, base_seconds=2, cap_seconds=60, rng=_Max())

    assert [no_jitter.delay_for(attempt) for attempt in range(1, 8)] == [2, 4, 8, 16, 32, 60, 60]


def test_retry_is_never_immediate() -> None:
    no_wait = RetryPolicy(max_attempts=5, base_seconds=2, cap_seconds=60, rng=_Min())

    assert no_wait.delay_for(1) == 1  # half of the base, at the least


def test_jitter_spreads_retries_of_jobs_that_failed_together() -> None:
    delays = {round(POLICY.delay_for(5), 3) for _ in range(20)}

    assert len(delays) > 10


class _Max(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return b


class _Min(random.Random):
    def uniform(self, a: float, b: float) -> float:
        return a
