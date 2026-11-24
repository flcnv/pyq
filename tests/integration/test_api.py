import hashlib
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.adapters.rate_limiter import RedisRateLimiter
from src.main import create_app
from tests.integration.pipeline import api_context
from tests.unit.context import app_with

FIXTURES = Path(__file__).parents[1] / "fixtures"
QR_PDF = ("invoice_with_qr.pdf", (FIXTURES / "invoice_with_qr.pdf").read_bytes(), "application/pdf")


@pytest.fixture
async def client(
    session_factory: async_sessionmaker[AsyncSession], rate_limiter: RedisRateLimiter
) -> AsyncIterator[AsyncClient]:
    """The app as wired in production: test database, real Redis."""
    app = app_with(create_app(), api_context(session_factory, rate_limiter))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def test_upload_stores_the_file_and_queues_a_job(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    response = await client.post("/invoices", files={"file": QR_PDF})

    assert response.status_code == 202
    polled = await client.get(response.headers["location"])
    assert polled.status_code == 200
    assert polled.json() == response.json()
    assert polled.json()["status"] == "pending"
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        job = await uow.jobs.get_by_content_sha256(hashlib.sha256(QR_PDF[1]).hexdigest())
        assert job is not None
        assert job.document_id is not None
        document = await uow.documents.get(job.document_id)
    assert document is not None
    assert document.content == QR_PDF[1]


async def test_reupload_returns_the_same_job(client: AsyncClient) -> None:
    first = await client.post("/invoices", files={"file": QR_PDF})
    again = await client.post("/invoices", files={"file": QR_PDF})

    assert (first.status_code, again.status_code) == (202, 202)
    assert again.json()["id"] == first.json()["id"]


async def test_mislabelled_file_is_400_and_nothing_is_stored(
    client: AsyncClient, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    png = (FIXTURES / "qr_example_1.png").read_bytes()
    response = await client.post("/invoices", files={"file": ("x.pdf", png, "application/pdf")})

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "unreadable_document"
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.jobs.get_by_content_sha256(hashlib.sha256(png).hexdigest()) is None
