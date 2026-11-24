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
