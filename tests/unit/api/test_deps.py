import pytest
from httpx import ASGITransport, AsyncClient

from src.main import create_app


async def test_a_missing_context_fails_with_a_clear_error() -> None:
    app = create_app()  # no lifespan run, no `app_with(...)`
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        with pytest.raises(RuntimeError, match="lifespan didn't run"):
            await client.get("/health")
