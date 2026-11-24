import asyncio
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from alembic import command
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from src.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.application.errors import JobAlreadyExistsError
from src.application.models.ingest_job import JobError, JobErrorDetail, JobStatus
from src.application.ports.document_storage import StoredDocument
from tests.integration.conftest import alembic_config
from tests.unit.application.jobs import make_document, make_job

type SessionFactory = async_sessionmaker[AsyncSession]

FAILED = JobError(
    code="invalid_invoice",
    message="Invoice failed validation with 1 error(s)",
    details=(
        JobErrorDetail(code="invalid_iban", message="bad", field="creditor.iban", severity="error"),
    ),
)


@pytest.fixture
async def uow(session_factory: SessionFactory) -> AsyncIterator[SqlAlchemyUnitOfWork]:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        yield uow


async def test_document_round_trip(uow: SqlAlchemyUnitOfWork) -> None:
    document = make_document(content=b"%PDF-1.7 \x00\xff binary")
    await uow.documents.add(document)

    assert await uow.documents.get(document.id) == document


async def test_job_round_trip_including_the_error(uow: SqlAlchemyUnitOfWork) -> None:
    document = make_document()
    job = make_job(document, status=JobStatus.FAILED, attempts=2, error=FAILED)
    await uow.documents.add(document)
    stored = await uow.jobs.add(job)

    assert await uow.jobs.get(job.id) == stored
    assert await uow.jobs.get_by_content_sha256(document.sha256) == stored
    timestamps = {
        "run_after": job.run_after,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }
    assert replace(stored, **timestamps) == job  # everything else as given


async def test_new_jobs_are_timed_by_the_database_clock(uow: SqlAlchemyUnitOfWork) -> None:
    document = make_document()
    await uow.documents.add(document)
    long_ago = datetime(2000, 1, 1, tzinfo=UTC)  # what an app server's wrong clock might say
    job = make_job(document, run_after=long_ago, created_at=long_ago, updated_at=long_ago)

    stored = await uow.jobs.add(job)

    assert stored.run_after == stored.created_at == stored.updated_at
    assert abs(stored.created_at - datetime.now(UTC)) < timedelta(minutes=1)


async def test_one_job_per_file(uow: SqlAlchemyUnitOfWork) -> None:
    document = make_document()
    await uow.documents.add(document)
    await uow.jobs.add(make_job(document))

    with pytest.raises(JobAlreadyExistsError):
        await uow.jobs.add(make_job(document))


async def test_reset_failed_requeues_with_the_new_uploader(uow: SqlAlchemyUnitOfWork) -> None:
    document = make_document()
    job = make_job(document, status=JobStatus.FAILED, attempts=5, error=FAILED)
    await uow.documents.add(document)
    await uow.jobs.add(job)

    reset = await uow.jobs.reset_failed(job.id, client_id="198.51.100.1", request_id="req-2")

    assert reset is not None
    assert (reset.status, reset.attempts, reset.error) == (JobStatus.PENDING, 0, None)
    assert (reset.client_id, reset.request_id) == ("198.51.100.1", "req-2")


@pytest.mark.parametrize("status", [JobStatus.PENDING, JobStatus.PROCESSING])
async def test_reset_failed_leaves_other_jobs_alone(
    uow: SqlAlchemyUnitOfWork, status: JobStatus
) -> None:
    document = make_document()
    locked = {"locked_by": "w1", "locked_until": make_job().run_after}
    job = make_job(document, status=status, **(locked if status is JobStatus.PROCESSING else {}))
    await uow.documents.add(document)
    stored = await uow.jobs.add(job)

    assert await uow.jobs.reset_failed(job.id, client_id="x", request_id=None) is None
    assert await uow.jobs.get(job.id) == stored


async def test_nothing_is_saved_without_commit(session_factory: SessionFactory) -> None:
    document = make_document()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.documents.add(document)
        await uow.jobs.add(make_job(document))

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.documents.get(document.id) is None
        assert await uow.jobs.get_by_content_sha256(document.sha256) is None


async def test_document_and_job_are_saved_together_or_not_at_all(
    session_factory: SessionFactory,
) -> None:
    first = make_document()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.documents.add(first)
        await uow.jobs.add(make_job(first))
        await uow.commit()

    second = make_document(content=b"another upload of the same file", sha256=first.sha256)
    with pytest.raises(JobAlreadyExistsError):
        await enqueue(session_factory, second)  # the job is rejected: same hash

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.documents.get(second.id) is None  # rolled back with the job
        assert await uow.documents.get(first.id) == first


async def enqueue(session_factory: SessionFactory, document: StoredDocument) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.documents.add(document)
        await uow.jobs.add(make_job(document))
        await uow.commit()


async def test_done_job_must_have_an_invoice(session_factory: SessionFactory) -> None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        document = make_document()
        await uow.documents.add(document)
        with pytest.raises(Exception, match="ck_ingest_jobs_done_has_invoice"):
            await uow.jobs.add(make_job(document, status=JobStatus.DONE))


INSERT_INVOICE = text(
    "INSERT INTO invoices (id, created_at, source, content_sha256, creditor_iban, creditor_name,"
    " creditor_postal_code, creditor_town, creditor_country, currency, reference_type)"
    " VALUES (:id, now(), 'qr', :sha, 'CH6431961000004421557', 'n', '8000', 'Z', 'CH',"
    " 'CHF', 'NON')"
)
HASHED_INVOICE = "00000000-0000-4000-8000-000000000001"
UNHASHED_INVOICE = "00000000-0000-4000-8000-000000000002"


async def insert_invoices(database_url: URL) -> None:
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.begin() as connection:
        await connection.execute(INSERT_INVOICE, {"id": HASHED_INVOICE, "sha": "a" * 64})
        await connection.execute(INSERT_INVOICE, {"id": UNHASHED_INVOICE, "sha": None})
    await engine.dispose()


async def job_rows(database_url: URL) -> list[tuple[str, str, str]]:
    engine = create_async_engine(database_url, poolclass=NullPool)
    async with engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT content_sha256, status, invoice_id::text FROM ingest_jobs")
        )
        result = [(row[0], row[1], row[2]) for row in rows]
    await engine.dispose()
    return result


def test_migration_gives_every_hashed_invoice_a_done_job(database_url: URL) -> None:
    config = alembic_config(database_url)
    command.downgrade(config, "3a96ca5717d0")  # before the jobs table existed
    try:
        asyncio.run(insert_invoices(database_url))
        command.upgrade(config, "head")

        assert asyncio.run(job_rows(database_url)) == [("a" * 64, "done", HASHED_INVOICE)]
    finally:
        command.upgrade(config, "head")
