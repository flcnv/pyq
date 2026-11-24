from httpx import ASGITransport, AsyncClient

from src.config import Settings
from src.main import create_app
from tests.unit.context import app_with, make_context


async def test_health_returns_ok_and_the_build_version() -> None:
    app = app_with(create_app(), make_context(settings=Settings(git_sha="abc1234")))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "abc1234"}
