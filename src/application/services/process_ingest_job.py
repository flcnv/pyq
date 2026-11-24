import asyncio
import logging
from collections.abc import Coroutine
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from src.application.errors import (
    NoQrCodeFoundError,
    UnreadableDocumentError,
    UnusableLlmOutputError,
)
from src.application.job_errors import INTERNAL_ERROR, job_error_for, retries_exhausted
from src.application.llm_quota import LlmQuota
from src.application.models.ingest_job import IngestJob, JobError
from src.application.ports.clock import Clock
from src.application.ports.document_reader import Document, DocumentReader
from src.application.ports.document_storage import StoredDocument
from src.application.ports.extraction_failure_cache import ExtractionFailureCache
from src.application.ports.invoice_parser import InvoiceParser
from src.application.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from src.application.retry_policy import RetryPolicy, is_transient
from src.domain.duplicates import can_identify_payment, possible_duplicate_warning
from src.domain.errors import InvalidInvoiceError
from src.domain.models.invoice import Invoice
from src.domain.validation import ensure_valid

logger = logging.getLogger(__name__)

UNKNOWN_CLIENT = "unknown"


class JobOutcome(StrEnum):
    DONE = "done"
    FAILED = "failed"
    RETRY = "retry"  # transient failure: back to pending, due again after a backoff
    LOST = "lost"  # another worker owns the job now; our result was discarded


class ProcessIngestJob:
    """Turn one claimed job into an invoice, or into a failed job with a public error.

    QR code first; only when the document has no QR code at all does the LLM run, within the
    uploader's budget, and not for a file it already failed on in a way a retry can't fix.
    Both sources go through the same `ensure_valid`: LLM output is never trusted blindly.

    Finishing is fenced: the job is updated only if this worker still owns it, and the invoice
    is stored in the same transaction, so a worker whose lease ran out can't store a duplicate.

    Transient failures (see `is_transient`) are retried with backoff until the attempts run
    out; every other failure fails the job at once with its public error.
    """

    def __init__(
        self,
        *,
        worker_id: str,
        document_reader: DocumentReader,
        qr_parser: InvoiceParser,
        llm_parser: InvoiceParser,
        llm_quota: LlmQuota,
        llm_failure_cache: ExtractionFailureCache,
        unit_of_work: UnitOfWorkFactory,
        clock: Clock,
        retry_policy: RetryPolicy,
    ) -> None:
        self._worker_id = worker_id
        self._document_reader = document_reader
        self._qr_parser = qr_parser
        self._llm_parser = llm_parser
        self._llm_quota = llm_quota
        self._llm_failure_cache = llm_failure_cache
        self._unit_of_work = unit_of_work
        self._clock = clock
        self._retry_policy = retry_policy

    async def execute(self, job: IngestJob) -> JobOutcome:
        try:
            invoice = await self._invoice_from(job)
            return await _uninterrupted(self._complete(job, invoice))
        except Exception as error:  # every failure is recorded on the job, never lost
            if is_transient(error):
                return await _uninterrupted(self._retry_or_give_up(job, error))
            return await _uninterrupted(self._fail(job, self._public_error(error)))

    # --- Extraction ------------------------------------------------------------------------------

    async def _invoice_from(self, job: IngestJob) -> Invoice:
        content = await self._stored_document(job)
        document = await self._document_reader.read(content.content)
        return await self._extract(
            document,
            invoice_id=uuid4(),
            created_at=self._clock.now(),
            content_sha256=job.content_sha256,
            client_id=job.client_id or UNKNOWN_CLIENT,
        )

    async def _stored_document(self, job: IngestJob) -> StoredDocument:
        async with self._unit_of_work() as uow:
            stored = None if job.document_id is None else await uow.documents.get(job.document_id)
        if stored is None:
            raise UnreadableDocumentError("The uploaded file is no longer stored")
        return stored

    async def _extract(
        self,
        document: Document,
        *,
        invoice_id: UUID,
        created_at: datetime,
        content_sha256: str,
        client_id: str,
    ) -> Invoice:
        """The validated invoice from the QR code, or from the LLM if there is no QR code."""
        try:
            parsed = await self._qr_parser.parse(
                document, invoice_id=invoice_id, created_at=created_at
            )
        except NoQrCodeFoundError:
            logger.info("no QR code found, falling back to LLM")
        else:
            return ensure_valid(parsed)

        if not document.text.strip():
            # Nothing the LLM could read: fail fast instead of paying for an empty API call.
            raise UnreadableDocumentError("Document has no QR code and no readable text")
        return await self._extract_with_llm(
            document,
            invoice_id=invoice_id,
            created_at=created_at,
            content_sha256=content_sha256,
            client_id=client_id,
        )

    async def _extract_with_llm(
        self,
        document: Document,
        *,
        invoice_id: UUID,
        created_at: datetime,
        content_sha256: str,
        client_id: str,
    ) -> Invoice:
        known_failure = await self._llm_failure_cache.get(content_sha256)
        if known_failure is not None:
            logger.info("LLM already failed on this file, not retrying")
            raise known_failure

        await self._llm_quota.consume(client_id)
        try:
            parsed = await self._llm_parser.parse(
                document, invoice_id=invoice_id, created_at=created_at
            )
            return ensure_valid(parsed)
        except (UnusableLlmOutputError, InvalidInvoiceError) as failure:
            # Transient failures (timeouts, 5xx, 429) are not caught: they stay retryable.
            await self._llm_failure_cache.remember(content_sha256, failure)
            raise

    # --- Finishing (fenced) ----------------------------------------------------------------------

    async def _complete(self, job: IngestJob, invoice: Invoice) -> JobOutcome:
        async with self._unit_of_work() as uow:
            # The fenced update first: it also row-locks the job, so a competing worker's own
            # update waits for our commit and then matches nothing.
            if not await uow.jobs.complete(
                job.id, worker_id=self._worker_id, invoice_id=invoice.id
            ):
                return self._lost(job, "completed")
            invoice = await self._with_possible_duplicate(uow, invoice)
            await uow.invoices.add(invoice)
            await uow.commit()
        logger.info(
            "invoice ingested",
            extra={
                "invoice_id": str(invoice.id),
                "source": invoice.source.value,
                "warning_count": len(invoice.warnings),
            },
        )
        return JobOutcome.DONE

    async def _retry_or_give_up(self, job: IngestJob, error: Exception) -> JobOutcome:
        last_error = self._public_error(error)
        if job.attempts >= self._retry_policy.max_attempts:
            return await self._fail(job, retries_exhausted(job.attempts, last_error))
        delay = self._retry_policy.delay_for(job.attempts)
        async with self._unit_of_work() as uow:
            if not await uow.jobs.retry_later(
                job.id, worker_id=self._worker_id, delay_seconds=delay
            ):
                return self._lost(job, "retry")
            await uow.commit()
        logger.warning(
            "ingestion will be retried",
            extra={
                "error_code": last_error.code,
                "attempt": job.attempts,
                "retry_in_seconds": round(delay, 1),
            },
        )
        return JobOutcome.RETRY

    async def _fail(self, job: IngestJob, job_error: JobError) -> JobOutcome:
        async with self._unit_of_work() as uow:
            if not await uow.jobs.fail(job.id, worker_id=self._worker_id, error=job_error):
                return self._lost(job, "failed")
            await uow.commit()
        logger.info("ingestion failed", extra={"error_code": job_error.code})
        return JobOutcome.FAILED

    @staticmethod
    def _public_error(error: Exception) -> JobError:
        job_error = job_error_for(error)
        if job_error is None:
            # Unknown: almost always a bug. The traceback goes to the log only (the engine hides
            # SQL parameters, and errors here never carry document contents).
            logger.error("unexpected error while processing a job", exc_info=error)
            return INTERNAL_ERROR
        return job_error

    @staticmethod
    async def _with_possible_duplicate(uow: UnitOfWork, invoice: Invoice) -> Invoice:
        """The same bill in another file (a new scan, a re-export): stored, but flagged."""
        if not can_identify_payment(invoice):
            return invoice
        warning = possible_duplicate_warning(invoice, await uow.invoices.find_same_payment(invoice))
        if warning is None:
            return invoice
        return replace(invoice, warnings=(*invoice.warnings, warning))

    @staticmethod
    def _lost(job: IngestJob, outcome: str) -> JobOutcome:
        logger.warning(
            "job is owned by another worker now; result discarded",
            extra={"discarded_outcome": outcome, "attempts": job.attempts},
        )
        return JobOutcome.LOST


async def _uninterrupted[T](finishing: Coroutine[Any, Any, T]) -> T:
    """Run the finishing transaction to the end even if the job is cancelled meanwhile (worker
    shutdown), so a commit is never cut in half; the cancellation takes effect right after.

    Any: the coroutine's send and yield types, which don't matter here.
    """
    task = asyncio.ensure_future(finishing)
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task  # a few short database calls, each bounded by the driver's timeouts
        raise
