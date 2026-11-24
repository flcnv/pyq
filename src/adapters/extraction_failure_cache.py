import json
import logging
from typing import Any

from redis.asyncio import Redis
from redis.exceptions import RedisError

from src.application.errors import UnusableLlmOutputError
from src.application.ports.extraction_failure_cache import DeterministicFailure
from src.domain.errors import InvalidInvoiceError
from src.domain.models.validation_issue import IssueCode, IssueSeverity, ValidationIssue

logger = logging.getLogger(__name__)

KEY_PREFIX = "llm-failure"


class RedisExtractionFailureCache:
    """Failures as JSON under `llm-failure:{git_sha}:{fingerprint}:{sha256}`, expiring after `ttl`.

    Old entries stop being found, so their files are tried again, when either changes:
    - `fingerprint`: the request sent to Claude (model, prompt, schema, limits);
    - `git_sha`: the build, i.e. the code that maps and validates the answer.
    """

    def __init__(self, redis: Redis, *, git_sha: str, fingerprint: str, ttl_seconds: int) -> None:
        self._redis = redis
        self._key_prefix = f"{KEY_PREFIX}:{git_sha}:{fingerprint}"
        self._ttl_seconds = ttl_seconds

    def key(self, content_sha256: str) -> str:
        return f"{self._key_prefix}:{content_sha256}"

    async def get(self, content_sha256: str) -> DeterministicFailure | None:
        try:
            cached = await self._redis.get(self.key(content_sha256))
        except RedisError as error:
            logger.warning("LLM failure cache unavailable", extra={"error": type(error).__name__})
            return None
        return None if cached is None else _from_json(json.loads(cached))

    async def remember(self, content_sha256: str, failure: DeterministicFailure) -> None:
        value = json.dumps(_to_json(failure))
        try:
            await self._redis.set(self.key(content_sha256), value, ex=self._ttl_seconds)
        except RedisError as error:
            logger.warning("LLM failure cache unavailable", extra={"error": type(error).__name__})


# Any: JSON documents written and read only by this module.
def _to_json(failure: DeterministicFailure) -> dict[str, Any]:
    if isinstance(failure, InvalidInvoiceError):
        return {"kind": "invalid_invoice", "issues": [_issue_to_json(i) for i in failure.issues]}
    return {"kind": "unusable_output", "message": str(failure)}


def _from_json(data: dict[str, Any]) -> DeterministicFailure:
    if data["kind"] == "invalid_invoice":
        return InvalidInvoiceError(tuple(_issue_from_json(issue) for issue in data["issues"]))
    return UnusableLlmOutputError(data["message"])


def _issue_to_json(issue: ValidationIssue) -> dict[str, str]:
    return {
        "code": issue.code.value,
        "field": issue.field,
        "message": issue.message,
        "severity": issue.severity.value,
    }


def _issue_from_json(data: dict[str, str]) -> ValidationIssue:
    return ValidationIssue(
        code=IssueCode(data["code"]),
        field=data["field"],
        message=data["message"],
        severity=IssueSeverity(data["severity"]),
    )
