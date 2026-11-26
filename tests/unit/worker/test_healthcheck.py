import os
import time
from pathlib import Path

import pytest

from src.config import get_settings
from src.worker.healthcheck import MAX_HEARTBEAT_AGE_SECONDS, heartbeat_is_fresh, main


def beat(path: Path, seconds_ago: float) -> None:
    path.touch()
    then = time.time() - seconds_ago
    os.utime(path, (then, then))


@pytest.mark.parametrize(
    ("seconds_ago", "fresh"),
    [(0, True), (MAX_HEARTBEAT_AGE_SECONDS - 1, True), (MAX_HEARTBEAT_AGE_SECONDS + 1, False)],
    ids=["just now", "within the limit", "stale"],
)
def test_heartbeat_age_decides(tmp_path: Path, seconds_ago: float, fresh: bool) -> None:
    path = tmp_path / "worker.heartbeat"
    beat(path, seconds_ago)

    assert (
        heartbeat_is_fresh(path, now=time.time(), max_age_seconds=MAX_HEARTBEAT_AGE_SECONDS)
        is fresh
    )


def test_a_missing_heartbeat_is_unhealthy(tmp_path: Path) -> None:
    assert not heartbeat_is_fresh(tmp_path / "never-written", now=time.time(), max_age_seconds=60)


@pytest.mark.parametrize(("seconds_ago", "exit_code"), [(0, 0), (MAX_HEARTBEAT_AGE_SECONDS + 1, 1)])
def test_exit_code_follows_the_configured_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seconds_ago: float, exit_code: int
) -> None:
    path = tmp_path / "worker.heartbeat"
    beat(path, seconds_ago)
    monkeypatch.setenv("WORKER_HEARTBEAT_FILE", str(path))
    get_settings.cache_clear()
    try:
        assert main() == exit_code
    finally:
        get_settings.cache_clear()
