import asyncio

import pytest

from src.bootstrap import AppContext
from src.config import Settings
from src.worker.main import run
from src.worker.runner import Worker
from tests.unit.context import make_context
from tests.unit.logs import app_logs


class Closed:
    def __init__(self) -> None:
        self.count = 0

    async def close(self) -> None:
        self.count += 1


@pytest.fixture
def closed(monkeypatch: pytest.MonkeyPatch) -> Closed:
    """`run` gets a context of fakes; `closed.count` says how often it was closed."""
    closed = Closed()

    async def build_context(settings: Settings) -> AppContext:
        context = make_context(settings=settings)
        context.exit_stack.push_async_callback(closed.close)
        return context

    monkeypatch.setattr("src.worker.main.build_context", build_context)
    return closed


def stopped() -> asyncio.Event:
    stop = asyncio.Event()
    stop.set()
    return stop


async def test_closes_the_context_after_a_stop(closed: Closed) -> None:
    assert await run(Settings(), stopped()) == 0
    assert closed.count == 1


async def test_closes_the_context_when_the_worker_crashes(
    closed: Closed, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def crash(self: Worker, stop: asyncio.Event) -> None:
        raise RuntimeError("worker loop crashed")

    monkeypatch.setattr(Worker, "run", crash)

    with pytest.raises(RuntimeError, match="worker loop crashed"):
        await run(Settings(), asyncio.Event())
    assert closed.count == 1


async def test_a_failed_startup_exits_non_zero_and_logs_the_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def build_context(settings: Settings) -> AppContext:
        raise ConnectionError("no route to the database")

    monkeypatch.setattr("src.worker.main.build_context", build_context)

    with app_logs() as logs:
        exit_code = await run(Settings(), asyncio.Event())

    assert exit_code == 1
    [failure] = logs.json()
    assert failure["message"] == "worker failed to start"
    assert "ConnectionError" in failure["exception"]
