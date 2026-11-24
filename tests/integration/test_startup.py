import asyncio

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import URL

from src.config import Settings, get_settings
from src.main import create_app
from src.worker import main as worker_main
from tests.integration.pipeline import SessionFactory


@pytest.fixture
def settings(database_url: URL, session_factory: SessionFactory) -> Settings:
    """The real settings, pointed at the (emptied) test database."""
    return get_settings().model_copy(update={"postgres_db": database_url.database})


async def test_the_api_starts_and_stops_through_build_context(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("src.main.get_settings", lambda: settings)
    app = create_app()

    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            health = await client.get("/health")
            invoices = await client.get("/invoices")  # a real query on the test database

    assert health.status_code == 200
    assert invoices.status_code == 200
    assert invoices.json()["items"] == []


async def test_the_worker_starts_polls_and_stops_through_build_context(
    settings: Settings,
) -> None:
    stop = asyncio.Event()
    # After the first claim on the (empty) test database: a real start, poll and shutdown.
    asyncio.get_running_loop().call_later(0.2, stop.set)

    assert await worker_main.run(settings, stop) == 0
