import sys
import time
from pathlib import Path

from src.config import get_settings

# The loop beats every poll interval (1 s by default). Claiming can take longer when the database
# hangs (the driver's connect timeout), so allow a minute before calling the worker stuck.
MAX_HEARTBEAT_AGE_SECONDS = 60.0


def heartbeat_is_fresh(path: Path, *, now: float, max_age_seconds: float) -> bool:
    try:
        return now - path.stat().st_mtime <= max_age_seconds
    except OSError:  # not written yet, or unreadable
        return False


def main() -> int:
    """The worker container's healthcheck: exit 0 if the worker loop beat recently."""
    path = get_settings().worker_heartbeat_file
    fresh = heartbeat_is_fresh(path, now=time.time(), max_age_seconds=MAX_HEARTBEAT_AGE_SECONDS)
    return 0 if fresh else 1


if __name__ == "__main__":
    sys.exit(main())
