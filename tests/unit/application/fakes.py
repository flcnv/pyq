from dataclasses import replace
from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from src.application.errors import (
    JobAlreadyExistsError,
    RateLimiterUnavailableError,
    RateLimitExceededError,
)
from src.application.models.ingest_job import IngestJob, JobError, JobStatus
from src.application.ports.document_reader import Document
from src.application.ports.document_storage import StoredDocument
from src.application.ports.extraction_failure_cache import DeterministicFailure
from src.application.ports.invoice_repository import InvoicePage, InvoiceQuery
from src.application.ports.rate_limiter import RateLimit
from src.domain.models.invoice import Invoice


class FakeDocumentReader:
    def __init__(self, result: Document | Exception) -> None:
        self._result = result
        self.calls: list[bytes] = []

    async def read(self, content: bytes) -> Document:
        self.calls.append(content)
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


class FakeInvoiceParser:
    """Returns the configured invoice (with the caller's id and timestamp) or raises the error."""

    def __init__(self, result: Invoice | Exception) -> None:
        self._result = result
        self.calls: list[Document] = []

    async def parse(self, document: Document, *, invoice_id: UUID, created_at: datetime) -> Invoice:
        self.calls.append(document)
        if isinstance(self._result, Exception):
            raise self._result
        return replace(self._result, id=invoice_id, created_at=created_at)


class InMemoryInvoiceRepository:
    def __init__(self, invoices: tuple[Invoice, ...] = ()) -> None:
        self.invoices: dict[UUID, Invoice] = {invoice.id: invoice for invoice in invoices}

    async def add(self, invoice: Invoice) -> None:
        self.invoices[invoice.id] = invoice

    async def get(self, invoice_id: UUID) -> Invoice | None:
        return self.invoices.get(invoice_id)

    async def find_same_payment(self, invoice: Invoice) -> tuple[Invoice, ...]:
        return tuple(
            stored
            for stored in self.invoices.values()
            if stored.creditor.iban == invoice.creditor.iban
            and stored.reference == invoice.reference
            and stored.amount == invoice.amount
        )

    async def list(self, query: InvoiceQuery) -> InvoicePage:
        matches = [
            invoice
            for invoice in self.invoices.values()
            if query.creditor_iban in (None, invoice.creditor.iban)
            and query.currency in (None, invoice.amount.currency)
        ]
        matches.sort(key=lambda invoice: invoice.created_at, reverse=True)
        page = matches[query.offset : query.offset + query.limit]
        return InvoicePage(items=tuple(page), total=len(matches))


class FixedClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class FakeRateLimiter:
    """Counts hits per (limit name, key) in memory. No windows: counts never reset."""

    RETRY_AFTER_SECONDS = 42

    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.hits: dict[tuple[str, str], int] = {}

    async def hit(self, rate_limit: RateLimit, key: str) -> None:
        if self.unavailable:
            raise RateLimiterUnavailableError("limiter down")
        counter = (rate_limit.name, key)
        self.hits[counter] = self.hits.get(counter, 0) + 1
        if self.hits[counter] > rate_limit.limit:
            raise RateLimitExceededError(
                "Too many requests", retry_after_seconds=self.RETRY_AFTER_SECONDS
            )


class InMemoryExtractionFailureCache:
    def __init__(self) -> None:
        self.failures: dict[str, DeterministicFailure] = {}

    async def get(self, content_sha256: str) -> DeterministicFailure | None:
        return self.failures.get(content_sha256)

    async def remember(self, content_sha256: str, failure: DeterministicFailure) -> None:
        self.failures[content_sha256] = failure


class InMemoryDocumentStorage:
    def __init__(self) -> None:
        self.documents: dict[UUID, StoredDocument] = {}

    async def add(self, document: StoredDocument) -> None:
        self.documents[document.id] = document

    async def get(self, document_id: UUID) -> StoredDocument | None:
        return self.documents.get(document_id)


class InMemoryIngestJobRepository:
    def __init__(self) -> None:
        self.jobs: dict[UUID, IngestJob] = {}

    async def add(self, job: IngestJob) -> IngestJob:
        if any(stored.content_sha256 == job.content_sha256 for stored in self.jobs.values()):
            raise JobAlreadyExistsError("duplicate content")
        self.jobs[job.id] = job
        return job

    async def get(self, job_id: UUID) -> IngestJob | None:
        return self.jobs.get(job_id)

    async def get_by_content_sha256(self, content_sha256: str) -> IngestJob | None:
        return next(
            (job for job in self.jobs.values() if job.content_sha256 == content_sha256), None
        )

    async def reset_failed(
        self, job_id: UUID, *, client_id: str, request_id: str | None
    ) -> IngestJob | None:
        job = self.jobs.get(job_id)
        if job is None or job.status is not JobStatus.FAILED:
            return None
        reset = replace(
            job,
            status=JobStatus.PENDING,
            attempts=0,
            error=None,
            client_id=client_id,
            request_id=request_id,
        )
        self.jobs[job_id] = reset
        return reset


class InMemoryJobQueue(InMemoryIngestJobRepository):
    """The queue operations, with the real fence: `complete`/`fail` only apply to a job that is
    `processing` and locked by that worker. No clock: every pending job is due, and leases
    never expire (both are tested against Postgres)."""

    def __init__(self) -> None:
        super().__init__()
        self.retry_delays: list[float] = []

    async def claim(
        self, *, worker_id: str, lease_seconds: int, max_attempts: int
    ) -> IngestJob | None:
        due = sorted(
            (job for job in self.jobs.values() if job.status is JobStatus.PENDING),
            key=lambda job: (job.run_after, job.created_at),
        )
        if not due:
            return None
        claimed = replace(
            due[0],
            status=JobStatus.PROCESSING,
            locked_by=worker_id,
            locked_until=due[0].run_after,
            attempts=due[0].attempts + 1,
        )
        self.jobs[claimed.id] = claimed
        return claimed

    async def complete(self, job_id: UUID, *, worker_id: str, invoice_id: UUID) -> bool:
        return self._finish(job_id, worker_id, status=JobStatus.DONE, invoice_id=invoice_id)

    async def fail(self, job_id: UUID, *, worker_id: str, error: JobError) -> bool:
        return self._finish(job_id, worker_id, status=JobStatus.FAILED, error=error)

    async def retry_later(self, job_id: UUID, *, worker_id: str, delay_seconds: float) -> bool:
        if not self._finish(job_id, worker_id, status=JobStatus.PENDING):
            return False
        self.retry_delays.append(delay_seconds)
        return True

    async def fail_expired_at_max(self, *, max_attempts: int, error: JobError) -> int:
        return 0  # leases never expire without a clock

    async def release(self, job_id: UUID, *, worker_id: str) -> bool:
        job = self._owned(job_id, worker_id)
        if job is None:
            return False
        self.jobs[job_id] = replace(
            job,
            status=JobStatus.PENDING,
            attempts=max(job.attempts - 1, 0),
            locked_by=None,
            locked_until=None,
        )
        return True

    def _owned(self, job_id: UUID, worker_id: str) -> IngestJob | None:
        job = self.jobs.get(job_id)
        if job is None or job.status is not JobStatus.PROCESSING or job.locked_by != worker_id:
            return None
        return job

    def _finish(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        status: JobStatus,
        invoice_id: UUID | None = None,
        error: JobError | None = None,
    ) -> bool:
        job = self._owned(job_id, worker_id)
        if job is None:
            return False
        self.jobs[job_id] = replace(
            job,
            status=status,
            invoice_id=invoice_id,
            error=error,
            locked_by=None,
            locked_until=None,
        )
        return True


class InMemoryUnitOfWork:
    """Shares its repositories across every `async with`. Writes apply immediately: atomicity and
    rollback are tested against Postgres, not here. `commits` counts `commit()` calls."""

    def __init__(
        self,
        *,
        invoices: InMemoryInvoiceRepository | None = None,
        documents: InMemoryDocumentStorage | None = None,
        jobs: InMemoryJobQueue | None = None,
    ) -> None:
        self.invoices = invoices or InMemoryInvoiceRepository()
        self.documents = documents or InMemoryDocumentStorage()
        self.jobs = jobs or InMemoryJobQueue()
        self.commits = 0

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    def factory(self) -> Self:
        """Use as a `UnitOfWorkFactory`: every unit of work shares this one's state."""
        return self
