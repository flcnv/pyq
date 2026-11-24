from datetime import UTC, datetime
from typing import Any

from fastapi import FastAPI

from src.application.llm_quota import LlmQuota
from src.application.retry_policy import RetryPolicy
from src.bootstrap import AppContext
from src.config import Settings
from tests.unit.application.fakes import (
    FakeDocumentReader,
    FakeInvoiceParser,
    FakeRateLimiter,
    FixedClock,
    InMemoryExtractionFailureCache,
    InMemoryUnitOfWork,
)

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
NOT_USED = AssertionError("this port isn't expected to be used by the test")


def make_context(**overrides: Any) -> AppContext:
    """An `AppContext` of fakes, with nothing to close. Override any field by name."""
    rate_limiter = overrides.pop("rate_limiter", FakeRateLimiter())
    fields: dict[str, Any] = {
        "settings": Settings(),
        "unit_of_work": InMemoryUnitOfWork().factory,
        "clock": FixedClock(NOW),
        "rate_limiter": rate_limiter,
        "llm_quota": LlmQuota(rate_limiter, per_client_per_hour=100, per_day=100),
        "llm_failure_cache": InMemoryExtractionFailureCache(),
        "document_reader": FakeDocumentReader(NOT_USED),
        "qr_parser": FakeInvoiceParser(NOT_USED),
        "llm_parser": FakeInvoiceParser(NOT_USED),
        "retry_policy": RetryPolicy(max_attempts=5, base_seconds=2, cap_seconds=60),
    }
    unknown = overrides.keys() - fields.keys()
    assert not unknown, f"AppContext has no fields {sorted(unknown)}"
    return AppContext(**(fields | overrides))


def app_with(app: FastAPI, context: AppContext) -> FastAPI:
    """What the lifespan does in production. httpx's ASGITransport doesn't run the lifespan."""
    app.state.context = context
    return app
