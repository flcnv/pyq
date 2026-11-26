import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import URL, text
from sqlalchemy.exc import IntegrityError

from src.adapters.parsers.llm import ExtractionConfig, LlmInvoiceParser
from src.adapters.parsers.qr_bill import QrBillParser
from src.adapters.persistence.database import create_engine, create_session_factory
from src.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.adapters.rate_limiter import RedisRateLimiter
from src.application.errors import ExtractionFailedError, StorageUnavailableError
from src.application.models.ingest_job import IngestJob, JobStatus
from src.application.ports.document_reader import Document
from src.application.ports.invoice_parser import InvoiceParser
from src.application.retry_policy import RetryPolicy
from src.application.services.process_ingest_job import JobOutcome
from src.domain.models.invoice import Invoice
from src.main import create_app
from src.worker.runner import Worker
from tests.integration.conftest import CacheFactory
from tests.integration.pipeline import (
    LEASE_SECONDS,
    SessionFactory,
    api_context,
    document_reader,
    enqueue,
    job,
    processor,
    worker,
)
from tests.unit.adapters.claude_stub import EXAMPLE_1_EXTRACTION, ClaudeStub
from tests.unit.application.fakes import FakeDocumentReader, FakeInvoiceParser
from tests.unit.context import app_with
from tests.unit.domain.factories import make_invoice

FIXTURES = Path(__file__).parents[1] / "fixtures"
MAX_ATTEMPTS = 5
QR_PDF = (FIXTURES / "invoice_with_qr.pdf").read_bytes()
NO_QR_PDF = (FIXTURES / "invoice_without_qr.pdf").read_bytes()
NO_QR_WITH_REFERENCE_PDF = (FIXTURES / "invoice_without_qr_with_reference.pdf").read_bytes()
NO_QR_IMAGE_ONLY_PDF = (FIXTURES / "invoice_without_qr_image_only.pdf").read_bytes()


async def claim(session_factory: SessionFactory, worker_id: str) -> IngestJob | None:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        claimed = await uow.jobs.claim(
            worker_id=worker_id, lease_seconds=LEASE_SECONDS, max_attempts=MAX_ATTEMPTS
        )
        await uow.commit()
    return claimed


async def run_sql(session_factory: SessionFactory, sql: str, **params: object) -> None:
    async with session_factory() as session:
        await session.execute(text(sql), params)
        await session.commit()


async def expire_lease(session_factory: SessionFactory, job_id: UUID) -> None:
    await run_sql(
        session_factory,
        "UPDATE ingest_jobs SET locked_until = now() - interval '1 second' WHERE id = :id",
        id=job_id,
    )


def real_worker(
    session_factory: SessionFactory, llm_parser: InvoiceParser | None, **kwargs: object
) -> Worker:
    return worker(
        session_factory,
        document_reader=document_reader(),
        qr_parser=QrBillParser(),
        llm_parser=llm_parser,
        **kwargs,  # type: ignore[arg-type]
    )


# --- Claiming ------------------------------------------------------------------------------------


async def test_claim_locks_the_job_for_the_lease_and_counts_the_attempt(
    session_factory: SessionFactory,
) -> None:
    queued = await enqueue(session_factory, b"%PDF- one")

    claimed = await claim(session_factory, "worker-1")

    assert claimed is not None
    assert claimed.id == queued.id
    assert (claimed.status, claimed.attempts, claimed.locked_by) == (
        JobStatus.PROCESSING,
        1,
        "worker-1",
    )
    assert claimed.locked_until is not None
    lease = (claimed.locked_until - claimed.updated_at).total_seconds()
    assert lease == pytest.approx(LEASE_SECONDS)


async def test_nothing_to_claim_returns_none(session_factory: SessionFactory) -> None:
    assert await claim(session_factory, "worker-1") is None


async def test_concurrent_claims_never_get_the_same_job(session_factory: SessionFactory) -> None:
    queued = {(await enqueue(session_factory, f"%PDF- {n}".encode())).id for n in range(10)}

    claims = await asyncio.gather(*(claim(session_factory, f"worker-{n}") for n in range(20)))

    claimed_ids = [job.id for job in claims if job is not None]
    assert len(claimed_ids) == len(set(claimed_ids))  # nobody got a job twice
    assert set(claimed_ids) == queued  # every job went to exactly one worker


async def test_job_scheduled_for_later_is_not_claimed(session_factory: SessionFactory) -> None:
    queued = await enqueue(session_factory, b"%PDF- later")
    await run_sql(
        session_factory,
        "UPDATE ingest_jobs SET run_after = now() + interval '1 hour' WHERE id = :id",
        id=queued.id,
    )

    assert await claim(session_factory, "worker-1") is None


async def test_live_lease_is_not_reclaimed(session_factory: SessionFactory) -> None:
    await enqueue(session_factory, b"%PDF- busy")
    assert await claim(session_factory, "worker-1") is not None

    assert await claim(session_factory, "worker-2") is None


async def test_expired_lease_is_reclaimed_as_a_new_attempt(
    session_factory: SessionFactory,
) -> None:
    queued = await enqueue(session_factory, b"%PDF- crashed")
    await claim(session_factory, "worker-1")
    await expire_lease(session_factory, queued.id)  # worker-1 crashed

    reclaimed = await claim(session_factory, "worker-2")

    assert reclaimed is not None
    assert (reclaimed.id, reclaimed.locked_by, reclaimed.attempts) == (queued.id, "worker-2", 2)


# --- Fencing -------------------------------------------------------------------------------------


async def test_a_stale_workers_completion_is_rejected_and_writes_no_invoice(
    session_factory: SessionFactory,
) -> None:
    queued = await enqueue(session_factory, QR_PDF)
    stale = await claim(session_factory, "stale-worker")
    assert stale is not None
    await expire_lease(session_factory, queued.id)
    current = real_worker(session_factory, None, worker_id="current-worker")
    slow = processor(
        session_factory,
        document_reader=document_reader(),
        qr_parser=QrBillParser(),
        llm_parser=None,
        worker_id="stale-worker",
    )

    await current.run_until_idle()  # reclaims the job and completes it
    done = await job(session_factory, queued.id)
    outcome = await slow.execute(stale)  # the stale worker finally finishes its copy

    assert outcome is JobOutcome.LOST

    after = await job(session_factory, queued.id)
    assert (after.status, after.invoice_id) == (JobStatus.DONE, done.invoice_id)
    async with session_factory() as session:
        invoices = await session.scalar(text("SELECT count(*) FROM invoices"))
    assert invoices == 1


# --- End to end ----------------------------------------------------------------------------------


@pytest.fixture
async def client(
    session_factory: SessionFactory, rate_limiter: RedisRateLimiter
) -> AsyncIterator[AsyncClient]:
    app = app_with(create_app(), api_context(session_factory, rate_limiter))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


async def upload_and_process(
    client: AsyncClient, file: tuple[str, bytes, str], processing: Worker
) -> dict[str, object]:
    queued = await client.post("/invoices", files={"file": file}, headers={"X-Request-ID": "e2e"})
    assert queued.status_code == 202
    await processing.run_until_idle()
    polled = await client.get(queued.headers["location"])
    assert polled.status_code == 200
    body: dict[str, object] = polled.json()
    return body


async def test_qr_upload_is_processed_into_an_invoice(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    file = ("invoice_with_qr.pdf", QR_PDF, "application/pdf")
    ingestion = await upload_and_process(client, file, real_worker(session_factory, None))

    assert ingestion["status"] == "done"
    invoice = await client.get(f"/invoices/{ingestion['invoice_id']}")
    assert invoice.status_code == 200
    assert invoice.json()["source"] == "qr"
    assert invoice.json()["creditor"]["name"] == "Max Muster & Söhne"
    assert invoice.json()["amount"] == {"value": "50.00", "currency": "CHF"}
    again = await client.post("/invoices", files={"file": file})
    assert (again.status_code, again.json()["id"]) == (200, ingestion["id"])


async def test_no_qr_code_goes_through_the_llm_fallback(
    client: AsyncClient, session_factory: SessionFactory, make_cache: CacheFactory
) -> None:
    claude = ClaudeStub.returning(EXAMPLE_1_EXTRACTION)
    llm = LlmInvoiceParser(claude.client, ExtractionConfig(model="claude-test-model"))
    processing = real_worker(session_factory, llm, failure_cache=make_cache())

    ingestion = await upload_and_process(
        client, ("no_qr.pdf", NO_QR_PDF, "application/pdf"), processing
    )

    assert ingestion["status"] == "done"
    invoice = (await client.get(f"/invoices/{ingestion['invoice_id']}")).json()
    assert invoice["source"] == "llm"
    sent = claude.last_request["messages"][0]["content"]
    assert "Konto / Zahlbar an: CH64 3196 1000 0044 2155 7" in sent


async def test_llm_fallback_stores_the_qr_reference_from_the_text(
    client: AsyncClient, session_factory: SessionFactory, make_cache: CacheFactory
) -> None:
    claude = ClaudeStub.returning(EXAMPLE_1_EXTRACTION)
    llm = LlmInvoiceParser(claude.client, ExtractionConfig(model="claude-test-model"))
    processing = real_worker(session_factory, llm, failure_cache=make_cache())

    ingestion = await upload_and_process(
        client, ("ref.pdf", NO_QR_WITH_REFERENCE_PDF, "application/pdf"), processing
    )

    assert ingestion["status"] == "done"
    sent = claude.last_request["messages"][0]["content"]
    assert "Referenz: 00 00082 07791 22585 74212 86694" in sent
    invoice = (await client.get(f"/invoices/{ingestion['invoice_id']}")).json()
    assert invoice["reference"] == {"type": "QRR", "value": "000008207791225857421286694"}


async def test_llm_output_with_a_qr_iban_but_no_qr_reference_is_rejected(
    client: AsyncClient, session_factory: SessionFactory, make_cache: CacheFactory
) -> None:
    """What the LLM can honestly read from the text-only invoice: the QR-IBAN, no reference.
    The same `ensure_valid` as for QR codes rejects it (ch. 4.3.2: QR-IBAN requires QRR)."""
    without_reference = EXAMPLE_1_EXTRACTION | {"reference_type": "NON", "reference": None}
    claude = ClaudeStub.returning(without_reference)
    llm = LlmInvoiceParser(claude.client, ExtractionConfig(model="claude-test-model"))
    processing = real_worker(session_factory, llm, failure_cache=make_cache())

    ingestion = await upload_and_process(
        client, ("no_qr.pdf", NO_QR_PDF, "application/pdf"), processing
    )

    assert ingestion["status"] == "failed"
    error = ingestion["error"]
    assert isinstance(error, dict)
    assert error["code"] == "invalid_invoice"
    assert "qr_iban_requires_qrr" in [detail["code"] for detail in error["details"]]


async def test_no_qr_code_and_no_text_fails_as_unreadable_without_calling_the_llm(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    claude = ClaudeStub()  # no responses: any request would fail the test
    llm = LlmInvoiceParser(claude.client, ExtractionConfig(model="claude-test-model"))

    ingestion = await upload_and_process(
        client,
        ("scan.pdf", NO_QR_IMAGE_ONLY_PDF, "application/pdf"),
        real_worker(session_factory, llm),
    )

    error = ingestion["error"]
    assert isinstance(error, dict)
    assert (ingestion["status"], error["code"]) == ("failed", "unreadable_document")
    assert claude.requests == []


async def test_unusable_llm_output_fails_the_ingestion(
    client: AsyncClient, session_factory: SessionFactory, make_cache: CacheFactory
) -> None:
    claude = ClaudeStub.returning({"creditor_iban": "CH64"})
    llm = LlmInvoiceParser(claude.client, ExtractionConfig(model="claude-test-model"))
    processing = real_worker(session_factory, llm, failure_cache=make_cache())

    ingestion = await upload_and_process(
        client, ("no_qr.pdf", NO_QR_PDF, "application/pdf"), processing
    )

    assert ingestion["status"] == "failed"
    assert ingestion["error"] == {
        "code": "extraction_failed",
        "message": "Invoice extraction is unavailable right now",
        "details": [],
    }


async def test_no_qr_code_without_an_llm_key_fails_with_a_clear_reason(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    ingestion = await upload_and_process(
        client,
        ("no_qr.pdf", NO_QR_PDF, "application/pdf"),
        real_worker(session_factory, None),
    )

    assert ingestion["status"] == "failed"
    error = ingestion["error"]
    assert isinstance(error, dict)
    assert error["code"] == "llm_fallback_not_configured"


async def test_a_broken_pdf_fails_as_unreadable(
    client: AsyncClient, session_factory: SessionFactory
) -> None:
    ingestion = await upload_and_process(
        client,
        ("broken.pdf", b"%PDF-1.7 not really", "application/pdf"),
        real_worker(session_factory, None),
    )

    error = ingestion["error"]
    assert isinstance(error, dict)
    assert (ingestion["status"], error["code"]) == ("failed", "unreadable_document")


async def test_running_worker_processes_new_uploads_until_stopped(
    session_factory: SessionFactory,
) -> None:
    stop = asyncio.Event()
    running = asyncio.create_task(real_worker(session_factory, None).run(stop))
    queued = await enqueue(session_factory, QR_PDF)

    for _ in range(200):  # poll interval is 0.05 s
        if (await job(session_factory, queued.id)).status is JobStatus.DONE:
            break
        await asyncio.sleep(0.05)
    stop.set()
    await asyncio.wait_for(running, timeout=5)

    assert (await job(session_factory, queued.id)).status is JobStatus.DONE


# --- Retries and poison jobs ---------------------------------------------------------------------


def failing_worker(
    session_factory: SessionFactory, qr: FakeInvoiceParser, policy: RetryPolicy
) -> Worker:
    return worker(
        session_factory,
        document_reader=FakeDocumentReader(Document(pages=(b"page.png",), text="")),
        qr_parser=qr,
        llm_parser=None,
        retry_policy=policy,
    )


async def test_transient_failure_is_retried_after_a_backoff(
    session_factory: SessionFactory,
) -> None:
    policy = RetryPolicy(max_attempts=5, base_seconds=30, cap_seconds=60)
    qr = FakeInvoiceParser(ExtractionFailedError("LLM timeout"))
    queued = await enqueue(session_factory, b"%PDF- transient")

    await failing_worker(session_factory, qr, policy).run_until_idle()

    retried = await job(session_factory, queued.id)
    assert (retried.status, retried.attempts, retried.locked_by) == (JobStatus.PENDING, 1, None)
    wait = (retried.run_after - retried.updated_at).total_seconds()
    assert 15 <= wait <= 30  # half to all of base * 2^0
    await failing_worker(session_factory, qr, policy).run_until_idle()
    assert len(qr.calls) == 1  # not due yet: not claimed again


async def test_repeated_transient_failures_end_as_retries_exhausted(
    session_factory: SessionFactory,
) -> None:
    policy = RetryPolicy(max_attempts=3, base_seconds=0.02, cap_seconds=0.02)
    qr = FakeInvoiceParser(ExtractionFailedError("LLM timeout"))
    queued = await enqueue(session_factory, b"%PDF- keeps failing")

    for _ in range(50):
        await failing_worker(session_factory, qr, policy).run_until_idle()
        if (await job(session_factory, queued.id)).status is JobStatus.FAILED:
            break
        await asyncio.sleep(0.03)

    failed = await job(session_factory, queued.id)
    assert (failed.status, failed.attempts) == (JobStatus.FAILED, 3)
    assert failed.error is not None
    assert failed.error.code == "retries_exhausted"
    assert [detail.code for detail in failed.error.details] == ["extraction_failed"]
    assert len(qr.calls) == 3


async def test_poison_job_is_failed_instead_of_being_claimed_again(
    session_factory: SessionFactory,
) -> None:
    queued = await enqueue(session_factory, b"%PDF- kills its worker")
    await claim(session_factory, "crashed-worker")
    await run_sql(
        session_factory,
        "UPDATE ingest_jobs SET attempts = :max, locked_until = now() - interval '1 second'"
        " WHERE id = :id",
        id=queued.id,
        max=MAX_ATTEMPTS,
    )
    qr = FakeInvoiceParser(make_invoice())

    await failing_worker(
        session_factory, qr, RetryPolicy(max_attempts=MAX_ATTEMPTS, base_seconds=1, cap_seconds=1)
    ).run_until_idle()

    poisoned = await job(session_factory, queued.id)
    assert poisoned.status is JobStatus.FAILED
    assert poisoned.error is not None
    assert poisoned.error.code == "retries_exhausted"
    assert [detail.code for detail in poisoned.error.details] == ["lease_expired"]
    assert qr.calls == []  # never handed out again


async def test_unreachable_database_is_reported_as_storage_unavailable() -> None:
    engine = create_engine(
        URL.create("postgresql+asyncpg", username="u", host="127.0.0.1", port=1, database="d")
    )
    try:
        with pytest.raises(StorageUnavailableError):
            async with SqlAlchemyUnitOfWork(create_session_factory(engine)) as uow:
                await uow.jobs.get(uuid4())
    finally:
        await engine.dispose()


async def test_a_constraint_violation_is_not_mistaken_for_an_outage(
    session_factory: SessionFactory,
) -> None:
    invoice = make_invoice()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.invoices.add(invoice)
        with pytest.raises(IntegrityError):
            await uow.invoices.add(invoice)  # same primary key


# --- Graceful shutdown ---------------------------------------------------------------------------


class HeldParser(FakeInvoiceParser):
    """Holds the job inside processing until released."""

    def __init__(self, result: Invoice) -> None:
        super().__init__(result)
        self.inside = asyncio.Event()
        self.go_on = asyncio.Event()

    async def parse(self, document: Document, *, invoice_id: UUID, created_at: datetime) -> Invoice:
        self.inside.set()
        await self.go_on.wait()
        return await super().parse(document, invoice_id=invoice_id, created_at=created_at)


def held_worker(session_factory: SessionFactory, qr: HeldParser, shutdown_timeout: float) -> Worker:
    return worker(
        session_factory,
        document_reader=FakeDocumentReader(Document(pages=(b"page.png",), text="")),
        qr_parser=qr,
        llm_parser=None,
        shutdown_timeout=shutdown_timeout,
    )


async def test_shutdown_lets_the_in_flight_job_finish(session_factory: SessionFactory) -> None:
    queued = await enqueue(session_factory, b"%PDF- in flight")
    qr = HeldParser(make_invoice())
    stop = asyncio.Event()
    running = asyncio.create_task(held_worker(session_factory, qr, shutdown_timeout=5).run(stop))
    await asyncio.wait_for(qr.inside.wait(), timeout=5)

    stop.set()  # SIGTERM
    qr.go_on.set()  # the job finishes within the shutdown timeout
    await asyncio.wait_for(running, timeout=5)

    assert (await job(session_factory, queued.id)).status is JobStatus.DONE


async def test_job_overrunning_the_shutdown_timeout_is_released_for_another_worker(
    session_factory: SessionFactory,
) -> None:
    queued = await enqueue(session_factory, b"%PDF- too slow")
    qr = HeldParser(make_invoice())  # never let go on
    stop = asyncio.Event()
    running = asyncio.create_task(held_worker(session_factory, qr, shutdown_timeout=0.1).run(stop))
    await asyncio.wait_for(qr.inside.wait(), timeout=5)

    stop.set()
    await asyncio.wait_for(running, timeout=5)

    released = await job(session_factory, queued.id)
    assert (released.status, released.attempts, released.locked_by) == (
        JobStatus.PENDING,
        0,  # the deploy wasn't the job's fault
        None,
    )
    assert await claim(session_factory, "next-worker") is not None  # due at once, not after a lease


async def test_release_only_applies_to_the_owner(session_factory: SessionFactory) -> None:
    queued = await enqueue(session_factory, b"%PDF- owned")
    await claim(session_factory, "owner")

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert not await uow.jobs.release(queued.id, worker_id="someone-else")
        await uow.commit()

    assert (await job(session_factory, queued.id)).locked_by == "owner"
