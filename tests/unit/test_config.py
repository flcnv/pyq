import pytest
from pydantic import ValidationError

from src.config import Settings


def test_build_version_defaults_to_dev(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GIT_SHA", raising=False)

    assert Settings(_env_file=None).git_sha == "dev"


def test_build_version_comes_from_git_sha(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_SHA", "abc1234-dirty")

    assert Settings(_env_file=None).git_sha == "abc1234-dirty"


def test_upload_limit_is_in_megabytes() -> None:
    assert Settings(_env_file=None, max_upload_mb=5).max_upload_bytes == 5 * 1024 * 1024


@pytest.mark.parametrize(
    "limit",
    [
        {"max_upload_mb": 0},
        {"max_pdf_pages": 0},
        {"max_pdf_page_pixels": 0},
    ],
    ids=["upload size", "pages", "page pixels"],
)
def test_upload_and_pdf_limits_must_be_positive(limit: dict[str, int]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **limit)  # type: ignore[arg-type]  # keys are Settings fields


@pytest.mark.parametrize(
    "setting",
    [
        {"worker_concurrency": 0},
        {"job_max_attempts": 0},
        {"job_retry_base_seconds": 0},
        {"job_retry_cap_seconds": 0},
        {"worker_poll_interval_seconds": 0},
        {"worker_shutdown_timeout_seconds": -1},
        {"job_lease_margin_seconds": -1},
        {"uploads_per_client_per_minute": 0},
        {"llm_requests_per_client_per_hour": 0},
        {"llm_requests_per_day": 0},
        {"llm_failure_cache_ttl_seconds": 0},
        {"llm_timeout_seconds": 0},
        {"llm_max_retries": -1},
        {"api_port": 0},
        {"postgres_port": 65536},
        {"redis_port": 0},
        {"redis_db": -1},
    ],
    ids=lambda setting: next(iter(setting)),
)
def test_out_of_range_settings_fail_at_startup(setting: dict[str, float]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **setting)  # type: ignore[arg-type]  # keys are Settings fields


@pytest.mark.parametrize(
    "setting",
    [
        {"worker_concurrency": 1},
        {"job_max_attempts": 1},
        {"worker_shutdown_timeout_seconds": 0},
        {"job_lease_margin_seconds": 0},
        {"llm_max_retries": 0},
        {"redis_db": 0},
    ],
    ids=lambda setting: next(iter(setting)),
)
def test_boundary_settings_are_accepted(setting: dict[str, float]) -> None:
    Settings(_env_file=None, **setting)  # type: ignore[arg-type]  # keys are Settings fields
