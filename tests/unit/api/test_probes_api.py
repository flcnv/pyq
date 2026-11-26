from httpx import ASGITransport, AsyncClient, Response

from src.application.errors import StorageUnavailableError
from src.bootstrap import AppContext
from src.config import Settings
from src.main import create_app
from tests.unit.context import app_with, make_context

SETTINGS = Settings(git_sha="abc1234")


async def get(context: AppContext, path: str) -> Response:
    app = app_with(create_app(), context)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(path)


async def database_down() -> None:
    raise StorageUnavailableError("The database is unavailable") from OSError("refused")


async def test_ready_when_the_database_answers() -> None:
    response = await get(make_context(settings=SETTINGS), "/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "version": "abc1234"}


async def test_not_ready_is_503_when_the_database_is_down() -> None:
    response = await get(make_context(settings=SETTINGS, database_ready=database_down), "/ready")

    assert response.status_code == 503
    assert response.headers["Retry-After"] == "5"
    assert response.json()["error"]["code"] == "storage_unavailable"


async def test_health_stays_ok_when_the_database_is_down() -> None:
    """Liveness only: restarting the API wouldn't bring the database back."""
    response = await get(make_context(settings=SETTINGS, database_ready=database_down), "/health")

    assert response.status_code == 200
