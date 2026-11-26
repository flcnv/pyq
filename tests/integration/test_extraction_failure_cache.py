import logging

import pytest
from redis.asyncio import Redis

from src.adapters.extraction_failure_cache import RedisExtractionFailureCache
from src.application.errors import UnusableLlmOutputError
from src.domain.errors import InvalidInvoiceError
from src.domain.models.validation_issue import IssueCode, IssueSeverity, ValidationIssue
from tests.integration.conftest import FAILURE_CACHE_TTL_SECONDS, CacheFactory

SHA256 = "ab" * 32
ISSUE = ValidationIssue(
    code=IssueCode.INVALID_IBAN,
    field="creditor.iban",
    message="IBAN check digits are wrong",
    severity=IssueSeverity.ERROR,
)


async def test_remembers_a_validation_failure_with_its_issues(make_cache: CacheFactory) -> None:
    cache = make_cache()

    await cache.remember(SHA256, InvalidInvoiceError((ISSUE,)))
    cached = await cache.get(SHA256)

    assert isinstance(cached, InvalidInvoiceError)
    assert cached.issues == (ISSUE,)


async def test_remembers_unusable_output(make_cache: CacheFactory) -> None:
    cache = make_cache()

    await cache.remember(SHA256, UnusableLlmOutputError("The LLM returned unusable output"))
    cached = await cache.get(SHA256)

    assert isinstance(cached, UnusableLlmOutputError)
    assert str(cached) == "The LLM returned unusable output"


async def test_unknown_file_is_a_miss(make_cache: CacheFactory) -> None:
    assert await make_cache().get(SHA256) is None


@pytest.mark.parametrize(
    "stored",
    [
        b"not json",
        b"\xff\xfe",
        b"[]",
        b'{"kind": "invalid_invoice"}',
        b'{"kind": "invalid_invoice", "issues": [{"code": "renamed_code", "field": "f", '
        b'"message": "m", "severity": "error"}]}',
    ],
    ids=["not-json", "not-utf8", "not-an-object", "missing-issues", "unknown-issue-code"],
)
async def test_unreadable_entry_is_a_miss_and_logged(
    make_cache: CacheFactory, redis: Redis, caplog: pytest.LogCaptureFixture, stored: bytes
) -> None:
    cache = make_cache()
    await redis.set(cache.key(SHA256), stored, ex=FAILURE_CACHE_TTL_SECONDS)
    caplog.set_level(logging.WARNING)

    assert await cache.get(SHA256) is None
    assert [record.message for record in caplog.records] == ["LLM failure cache entry unreadable"]


async def test_another_extraction_config_does_not_see_the_failure(
    make_cache: CacheFactory,
) -> None:
    await make_cache("a").remember(SHA256, UnusableLlmOutputError("x"))

    assert await make_cache("a").get(SHA256) is not None
    assert await make_cache("b").get(SHA256) is None


async def test_a_new_build_does_not_see_the_failure(make_cache: CacheFactory) -> None:
    await make_cache(build="b1").remember(SHA256, UnusableLlmOutputError("x"))

    assert await make_cache(build="b1").get(SHA256) is not None
    assert await make_cache(build="b2").get(SHA256) is None


async def test_key_is_build_fingerprint_and_file_hash_and_expires(
    make_cache: CacheFactory, redis: Redis
) -> None:
    cache = make_cache(config="a", build="b1")
    await cache.remember(SHA256, UnusableLlmOutputError("x"))

    key = cache.key(SHA256)
    prefix, git_sha, fingerprint, file_hash = key.split(":")
    assert (prefix, fingerprint, file_hash) == ("llm-failure", "a", SHA256)
    assert git_sha.endswith("-b1")
    assert 0 < await redis.ttl(key) <= FAILURE_CACHE_TTL_SECONDS


async def test_unavailable_redis_misses_and_skips_the_write(
    unreachable_redis: Redis, caplog: pytest.LogCaptureFixture
) -> None:
    cache = RedisExtractionFailureCache(
        unreachable_redis, git_sha="dev", fingerprint="abc", ttl_seconds=60
    )
    caplog.set_level(logging.WARNING)

    await cache.remember(SHA256, UnusableLlmOutputError("x"))
    assert await cache.get(SHA256) is None

    assert [record.message for record in caplog.records] == ["LLM failure cache unavailable"] * 2
