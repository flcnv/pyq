from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.adapters.clock import SystemClock
from src.adapters.documents import PdfiumDocumentReader
from src.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.application.llm_quota import LlmQuota
from src.application.models.ingest_job import IngestJob
from src.application.ports.document_reader import DocumentReader, DocumentType
from src.application.ports.extraction_failure_cache import ExtractionFailureCache
from src.application.ports.invoice_parser import InvoiceParser
from src.application.ports.rate_limiter import RateLimiter
from src.application.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from src.application.retry_policy import RetryPolicy
from src.application.services.enqueue_ingestion import EnqueueIngestion
from src.application.services.process_ingest_job import ProcessIngestJob
from src.bootstrap import AppContext
from src.config import Settings, get_settings
from src.worker.runner import Worker
from tests.unit.application.fakes import (
    FakeRateLimiter,
    FixedClock,
    InMemoryExtractionFailureCache,
)
from tests.unit.context import make_context

type SessionFactory = async_sessionmaker[AsyncSession]

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
LEASE_SECONDS = 300
RETRY_POLICY = RetryPolicy(max_attempts=5, base_seconds=2, cap_seconds=60)


def document_reader() -> PdfiumDocumentReader:
    """The real reader with the default limits (not `.env`): a changed default shows up in tests."""
    defaults = Settings.model_construct()
    return PdfiumDocumentReader(
        max_pages=defaults.max_pdf_pages, max_page_pixels=defaults.max_pdf_page_pixels
    )


def unit_of_work(session_factory: SessionFactory) -> UnitOfWorkFactory:
    def factory() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    return factory


def api_context(session_factory: SessionFactory, rate_limiter: RateLimiter) -> AppContext:
    """What the API uses in production: real settings and clock, the test database, real Redis."""
    return make_context(
        settings=get_settings(),
        unit_of_work=unit_of_work(session_factory),
        clock=SystemClock(),
        rate_limiter=rate_limiter,
    )


async def enqueue(
    session_factory: SessionFactory,
    content: bytes,
    document_type: DocumentType = DocumentType.PDF,
    client_id: str = "203.0.113.7",
) -> IngestJob:
    service = EnqueueIngestion(unit_of_work=unit_of_work(session_factory), clock=FixedClock(NOW))
    return await service.execute(content, document_type, client_id=client_id, request_id="req-1")


def processor(
    session_factory: SessionFactory,
    *,
    document_reader: DocumentReader,
    qr_parser: InvoiceParser,
    llm_parser: InvoiceParser,
    failure_cache: ExtractionFailureCache | None = None,
    worker_id: str = "worker-1",
    retry_policy: RetryPolicy = RETRY_POLICY,
) -> ProcessIngestJob:
    """The real job processing on the real database; the parsers are the caller's choice."""
    return ProcessIngestJob(
        worker_id=worker_id,
        document_reader=document_reader,
        qr_parser=qr_parser,
        llm_parser=llm_parser,
        llm_quota=LlmQuota(FakeRateLimiter(), per_client_per_hour=100, per_day=100),
        llm_failure_cache=failure_cache or InMemoryExtractionFailureCache(),
        unit_of_work=unit_of_work(session_factory),
        clock=FixedClock(NOW),
        retry_policy=retry_policy,
    )


def worker(
    session_factory: SessionFactory,
    *,
    document_reader: DocumentReader,
    qr_parser: InvoiceParser,
    llm_parser: InvoiceParser,
    failure_cache: ExtractionFailureCache | None = None,
    worker_id: str = "worker-1",
    concurrency: int = 2,
    retry_policy: RetryPolicy = RETRY_POLICY,
    shutdown_timeout: float = 5,
) -> Worker:
    """A worker running `processor(...)` on the real queue."""
    process = processor(
        session_factory,
        document_reader=document_reader,
        qr_parser=qr_parser,
        llm_parser=llm_parser,
        failure_cache=failure_cache,
        worker_id=worker_id,
        retry_policy=retry_policy,
    )
    return Worker(
        worker_id=worker_id,
        unit_of_work=unit_of_work(session_factory),
        process=process.execute,
        lease_seconds=LEASE_SECONDS,
        max_attempts=retry_policy.max_attempts,
        shutdown_timeout=shutdown_timeout,
        concurrency=concurrency,
        poll_interval=0.05,
    )


async def job(session_factory: SessionFactory, job_id: UUID) -> IngestJob:
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored = await uow.jobs.get(job_id)
    assert stored is not None
    return stored
