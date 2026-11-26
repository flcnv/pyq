import logging
import random
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from src.application.errors import (
    ExtractionFailedError,
    NoQrCodeFoundError,
    StorageUnavailableError,
    UnreadableDocumentError,
    UnusableLlmOutputError,
)
from src.application.llm_quota import LlmQuota
from src.application.models.ingest_job import IngestJob, JobStatus
from src.application.ports.document_reader import Document
from src.application.retry_policy import RetryPolicy
from src.application.services.process_ingest_job import JobOutcome, ProcessIngestJob
from src.domain.errors import InvalidQrPayloadError
from src.domain.models.amount import Currency
from src.domain.models.invoice import Invoice, InvoiceSource
from src.domain.models.validation_issue import IssueCode, IssueSeverity, ValidationIssue
from tests.unit.application.fakes import (
    FakeDocumentReader,
    FakeInvoiceParser,
    FakeRateLimiter,
    FixedClock,
    InMemoryExtractionFailureCache,
    InMemoryUnitOfWork,
)
from tests.unit.application.jobs import make_document, make_job
from tests.unit.domain.factories import QR_IBAN, make_invoice

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
WORKER = "worker-1"
CLIENT = "203.0.113.7"
LLM_PER_CLIENT = 2
LLM_PER_DAY = 3
DOCUMENT = Document(pages=(b"page-1.png",), text="Invoice 42, total CHF 50.00")
QR_INVOICE = make_invoice()
LLM_INVOICE = replace(make_invoice(), source=InvoiceSource.LLM)
NO_QR = NoQrCodeFoundError()
MALFORMED_QR = InvalidQrPayloadError(
    (
        ValidationIssue(
            code=IssueCode.MISSING_TRAILER,
            field="trailer",
            message="Line 31 must be the trailer 'EPD'",
            severity=IssueSeverity.ERROR,
        ),
    )
)
INVALID_LLM_INVOICE = replace(make_invoice(amount=Decimal("-50.00")), source=InvoiceSource.LLM)
MAX_ATTEMPTS = 3
RETRY_POLICY = RetryPolicy(
    max_attempts=MAX_ATTEMPTS, base_seconds=2, cap_seconds=60, rng=random.Random(7)
)


@dataclass
class Setup:
    service: ProcessIngestJob
    reader: FakeDocumentReader
    qr_parser: FakeInvoiceParser
    llm_parser: FakeInvoiceParser  # never called when the service got no LLM
    uow: InMemoryUnitOfWork
    limiter: FakeRateLimiter
    failure_cache: InMemoryExtractionFailureCache


def setup(
    *,
    document: Document | Exception = DOCUMENT,
    qr: Invoice | Exception = QR_INVOICE,
    llm: Invoice | Exception | None = LLM_INVOICE,  # None: no LLM configured
    limiter: FakeRateLimiter | None = None,
    uow: InMemoryUnitOfWork | None = None,
) -> Setup:
    reader = FakeDocumentReader(document)
    qr_parser = FakeInvoiceParser(qr)
    llm_parser = FakeInvoiceParser(LLM_INVOICE if llm is None else llm)
    limiter = limiter or FakeRateLimiter()
    failure_cache = InMemoryExtractionFailureCache()
    uow = uow or InMemoryUnitOfWork()
    service = ProcessIngestJob(
        worker_id=WORKER,
        document_reader=reader,
        qr_parser=qr_parser,
        llm_parser=None if llm is None else llm_parser,
        llm_quota=LlmQuota(limiter, per_client_per_hour=LLM_PER_CLIENT, per_day=LLM_PER_DAY),
        llm_failure_cache=failure_cache,
        unit_of_work=uow.factory,
        clock=FixedClock(NOW),
        retry_policy=RETRY_POLICY,
    )
    return Setup(service, reader, qr_parser, llm_parser, uow, limiter, failure_cache)


def claimed_job(
    s: Setup, content: bytes = b"%PDF-1.7 file", client_id: str | None = CLIENT
) -> IngestJob:
    """A stored document with its job, claimed by WORKER."""
    document = make_document(content=content)
    job = make_job(
        document,
        status=JobStatus.PROCESSING,
        attempts=1,
        locked_by=WORKER,
        locked_until=NOW,
        client_id=client_id,
    )
    s.uow.documents.documents[document.id] = document
    s.uow.jobs.jobs[job.id] = job
    return job


async def process(s: Setup, content: bytes = b"%PDF-1.7 file", **kwargs: str | None) -> IngestJob:
    """Process a fresh job for `content`; returns the job as stored afterwards."""
    job = claimed_job(s, content, **kwargs)
    await s.service.execute(job)
    return s.uow.jobs.jobs[job.id]


def error_code(job: IngestJob) -> str | None:
    return job.error.code if job.error else None


# --- Success -------------------------------------------------------------------------------------


async def test_qr_invoice_is_stored_and_the_job_done_without_calling_the_llm() -> None:
    s = setup()

    job = await process(s)

    assert job.status is JobStatus.DONE
    assert job.invoice_id is not None
    invoice = s.uow.invoices.invoices[job.invoice_id]
    assert (invoice.source, invoice.created_at) == (InvoiceSource.QR, NOW)
    assert (job.locked_by, job.locked_until, job.error) == (None, None, None)
    assert s.llm_parser.calls == []


async def test_falls_back_to_the_llm_when_there_is_no_qr_code() -> None:
    s = setup(qr=NO_QR)

    job = await process(s)

    assert job.status is JobStatus.DONE
    assert job.invoice_id is not None
    assert s.uow.invoices.invoices[job.invoice_id].source is InvoiceSource.LLM
    assert s.llm_parser.calls == [DOCUMENT]


async def test_stores_invoice_with_warnings() -> None:
    s = setup(qr=make_invoice(currency=Currency.EUR))

    job = await process(s)

    assert job.invoice_id is not None
    invoice = s.uow.invoices.invoices[job.invoice_id]
    assert [warning.code for warning in invoice.warnings] == [IssueCode.EUR_QR_REFERENCE_DEPRECATED]


async def test_stores_the_validated_canonical_amount() -> None:
    s = setup(qr=make_invoice(amount=Decimal("50")))

    job = await process(s)

    assert job.invoice_id is not None
    assert str(s.uow.invoices.invoices[job.invoice_id].amount.value) == "50.00"


async def test_same_bill_in_another_file_is_flagged_as_a_possible_duplicate() -> None:
    s = setup()
    first = await process(s, b"%PDF-1.7 scan 1")

    second = await process(s, b"%PDF-1.7 scan 2")

    assert second.invoice_id is not None
    [warning] = s.uow.invoices.invoices[second.invoice_id].warnings
    assert warning.code is IssueCode.POSSIBLE_DUPLICATE
    assert warning.related_invoice_ids == (first.invoice_id,)


# --- Deterministic failures: the job fails with the public error ---------------------------------


async def test_invalid_qr_code_fails_the_job_without_asking_the_llm() -> None:
    s = setup(qr=MALFORMED_QR)

    job = await process(s)

    assert job.status is JobStatus.FAILED
    assert job.error is not None
    assert job.error.code == "invalid_qr_payload"
    assert [detail.code for detail in job.error.details] == ["missing_trailer"]
    assert s.llm_parser.calls == []
    assert s.uow.invoices.invoices == {}


async def test_qr_invoice_failing_validation_fails_with_every_issue() -> None:
    s = setup(qr=make_invoice(iban="CH6431961000004421558"))

    job = await process(s)

    assert job.error is not None
    assert job.error.code == "invalid_invoice"
    [detail] = job.error.details
    assert (detail.code, detail.field, detail.severity) == (
        "invalid_iban",
        "creditor.iban",
        "error",
    )
    assert s.uow.invoices.invoices == {}


async def test_llm_output_goes_through_the_same_validation() -> None:
    s = setup(qr=NO_QR, llm=INVALID_LLM_INVOICE)

    job = await process(s)

    assert job.error is not None
    assert job.error.code == "invalid_invoice"
    assert [detail.code for detail in job.error.details] == ["amount_out_of_range"]


async def test_unusable_llm_output_fails_with_extraction_failed() -> None:
    s = setup(qr=NO_QR, llm=UnusableLlmOutputError("not the schema"))

    job = await process(s)

    assert job.error is not None
    assert (job.error.code, job.error.message) == (
        "extraction_failed",
        "Invoice extraction is unavailable right now",
    )


async def test_unreadable_document_fails_before_any_parser() -> None:
    s = setup(document=UnreadableDocumentError("Not a valid PDF"))

    job = await process(s)

    assert job.error is not None
    assert (job.error.code, job.error.message) == ("unreadable_document", "Not a valid PDF")
    assert s.qr_parser.calls == []
    assert s.llm_parser.calls == []


@pytest.mark.parametrize("text", ["", "  \n\t "], ids=["empty", "whitespace"])
async def test_no_qr_code_and_no_text_fails_without_calling_the_llm(text: str) -> None:
    s = setup(document=Document(pages=(b"page-1.png",), text=text), qr=NO_QR)

    job = await process(s)

    assert error_code(job) == "unreadable_document"
    assert s.llm_parser.calls == []


async def test_missing_document_fails_as_unreadable() -> None:
    s = setup()
    job = claimed_job(s)
    s.uow.documents.documents.clear()  # e.g. deleted by a retention job

    await s.service.execute(job)

    assert error_code(s.uow.jobs.jobs[job.id]) == "unreadable_document"


async def test_no_llm_configured_fails_with_its_own_code_without_using_the_budget() -> None:
    s = setup(qr=NO_QR, llm=None)

    job = await process(s)

    assert error_code(job) == "llm_fallback_not_configured"
    assert s.limiter.hits == {}


async def test_no_llm_configured_fails_at_once_even_when_the_budget_is_unavailable() -> None:
    s = setup(qr=NO_QR, llm=None, limiter=FakeRateLimiter(unavailable=True))

    job = await process(s)

    assert job.status is JobStatus.FAILED  # not retried as a transient outage
    assert error_code(job) == "llm_fallback_not_configured"


async def test_no_llm_configured_is_not_hidden_by_a_spent_budget() -> None:
    s = setup(qr=NO_QR, llm=None)
    for number in range(LLM_PER_CLIENT + 1):
        await process(s, f"file {number}".encode())

    job = await process(s, b"one more file")

    assert error_code(job) == "llm_fallback_not_configured"


async def test_unexpected_error_fails_with_internal_error_and_logs_the_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    s = setup(qr=RuntimeError("a bug"))
    caplog.set_level(logging.ERROR)

    job = await process(s)

    assert job.error is not None
    assert (job.error.code, job.error.message) == ("internal_error", "An unexpected error occurred")
    [record] = caplog.records
    assert record.exc_info is not None
    assert "a bug" not in job.error.message  # the public error never carries internals


# --- LLM budget ----------------------------------------------------------------------------------


async def test_qr_invoices_never_use_the_llm_budget() -> None:
    s = setup()

    await process(s)

    assert s.limiter.hits == {}


async def test_llm_counts_against_the_uploaders_and_the_global_budget() -> None:
    s = setup(qr=NO_QR)

    await process(s, client_id="198.51.100.9")

    assert s.limiter.hits == {("llm_client", "198.51.100.9"): 1, ("llm_global", "all"): 1}


async def test_jobs_without_a_client_count_as_unknown() -> None:
    s = setup(qr=NO_QR)

    await process(s, client_id=None)

    assert ("llm_client", "unknown") in s.limiter.hits


async def test_client_over_budget_fails_without_an_llm_call() -> None:
    s = setup(qr=NO_QR)
    for number in range(LLM_PER_CLIENT):
        await process(s, f"file {number}".encode())

    job = await process(s, b"one file too many")

    assert error_code(job) == "llm_quota_exceeded"
    assert len(s.llm_parser.calls) == LLM_PER_CLIENT


async def test_global_budget_applies_across_clients() -> None:
    s = setup(qr=NO_QR)
    for number in range(LLM_PER_DAY):
        await process(s, f"file {number}".encode(), client_id=f"client {number}")

    job = await process(s, b"new client, new file", client_id="fresh client")

    assert error_code(job) == "llm_quota_exceeded"
    assert len(s.llm_parser.calls) == LLM_PER_DAY


async def test_unavailable_budget_blocks_the_llm_but_not_qr_invoices() -> None:
    qr_setup = setup(limiter=FakeRateLimiter(unavailable=True))
    llm_setup = setup(qr=NO_QR, limiter=FakeRateLimiter(unavailable=True))

    assert (await process(qr_setup)).status is JobStatus.DONE
    assert (await process(llm_setup)).status is JobStatus.PENDING  # retried later
    assert llm_setup.llm_parser.calls == []


# --- Remembered LLM failures ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "llm",
    [
        pytest.param(INVALID_LLM_INVOICE, id="fails domain validation"),
        pytest.param(UnusableLlmOutputError("not the schema"), id="unusable output"),
    ],
)
async def test_deterministic_llm_failure_is_remembered_for_the_file(
    llm: Invoice | Exception,
) -> None:
    s = setup(qr=NO_QR, llm=llm)

    job = await process(s)

    assert set(s.failure_cache.failures) == {job.content_sha256}


@pytest.mark.parametrize(
    ("qr", "llm", "limiter"),
    [
        pytest.param(NO_QR, ExtractionFailedError("timeout"), None, id="timeout or 5xx"),
        pytest.param(NO_QR, LLM_INVOICE, FakeRateLimiter(unavailable=True), id="budget down"),
        pytest.param(MALFORMED_QR, LLM_INVOICE, None, id="invalid QR code"),
    ],
)
async def test_other_failures_are_not_remembered(
    qr: Invoice | Exception, llm: Invoice | Exception, limiter: FakeRateLimiter | None
) -> None:
    s = setup(qr=qr, llm=llm, limiter=limiter)

    await process(s)

    assert s.failure_cache.failures == {}


async def test_budget_rejection_is_not_remembered() -> None:
    s = setup(qr=NO_QR)
    for number in range(LLM_PER_CLIENT):
        await process(s, f"file {number}".encode())

    await process(s, b"over budget")

    assert len(s.failure_cache.failures) == 0


async def test_known_failure_is_replayed_without_calling_the_llm_or_spending_budget() -> None:
    s = setup(qr=NO_QR, llm=INVALID_LLM_INVOICE)
    first = await process(s)
    budget_after_first = dict(s.limiter.hits)
    requeued = replace(
        first, status=JobStatus.PROCESSING, error=None, locked_by=WORKER, locked_until=NOW
    )
    s.uow.jobs.jobs[first.id] = requeued  # re-uploaded and claimed again

    await s.service.execute(requeued)

    again = s.uow.jobs.jobs[first.id]
    assert again.error == first.error
    assert len(s.llm_parser.calls) == 1
    assert s.limiter.hits == budget_after_first


# --- Fencing: a worker that lost the job never writes ---------------------------------------------


async def test_result_is_discarded_when_another_worker_owns_the_job() -> None:
    s = setup()
    job = claimed_job(s)
    s.uow.jobs.jobs[job.id] = replace(job, locked_by="worker-2")  # our lease ran out; retaken

    outcome = await s.service.execute(job)

    assert outcome is JobOutcome.LOST
    assert s.uow.invoices.invoices == {}
    assert s.uow.jobs.jobs[job.id].locked_by == "worker-2"


async def test_failure_is_discarded_when_the_job_is_already_done() -> None:
    s = setup(qr=MALFORMED_QR)
    job = claimed_job(s)
    s.uow.jobs.jobs[job.id] = replace(
        job, status=JobStatus.DONE, invoice_id=uuid4(), locked_by=None, locked_until=None
    )

    assert await s.service.execute(job) is JobOutcome.LOST
    assert s.uow.jobs.jobs[job.id].status is JobStatus.DONE


# --- Retries --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("qr", "llm", "limiter"),
    [
        pytest.param(NO_QR, ExtractionFailedError("timeout"), None, id="LLM timeout or 5xx"),
        pytest.param(NO_QR, LLM_INVOICE, FakeRateLimiter(unavailable=True), id="budget down"),
        pytest.param(StorageUnavailableError("db down"), LLM_INVOICE, None, id="database down"),
    ],
)
async def test_transient_failure_goes_back_to_pending_after_a_backoff(
    qr: Invoice | Exception, llm: Invoice | Exception, limiter: FakeRateLimiter | None
) -> None:
    s = setup(qr=qr, llm=llm, limiter=limiter)
    job = claimed_job(s)

    outcome = await s.service.execute(job)

    after = s.uow.jobs.jobs[job.id]
    assert outcome is JobOutcome.RETRY
    assert (after.status, after.attempts, after.error, after.locked_by) == (
        JobStatus.PENDING,
        1,
        None,
        None,
    )
    assert s.uow.jobs.retry_delays == [pytest.approx(RETRY_POLICY.delay_for(1), abs=1)]
    assert s.uow.invoices.invoices == {}


async def test_transient_failure_on_the_last_attempt_is_retries_exhausted() -> None:
    s = setup(qr=NO_QR, llm=ExtractionFailedError("timeout"))
    job = claimed_job(s)
    last_attempt = replace(job, attempts=MAX_ATTEMPTS)
    s.uow.jobs.jobs[job.id] = last_attempt

    outcome = await s.service.execute(last_attempt)

    after = s.uow.jobs.jobs[job.id]
    assert outcome is JobOutcome.FAILED
    assert after.error is not None
    assert after.error.code == "retries_exhausted"
    assert after.error.message == "Processing failed 3 times; upload the file again to retry"
    [last] = after.error.details
    assert (last.code, last.message) == (
        "extraction_failed",
        "Invoice extraction is unavailable right now",
    )


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(UnusableLlmOutputError("prose"), id="unusable LLM output"),
        pytest.param(RuntimeError("a bug"), id="unknown error"),
    ],
)
async def test_deterministic_and_unknown_failures_are_never_retried(error: Exception) -> None:
    s = setup(qr=NO_QR, llm=error)

    job = await process(s)

    assert job.status is JobStatus.FAILED
    assert s.uow.jobs.retry_delays == []


# --- Logs ----------------------------------------------------------------------------------------


async def test_logs_never_contain_the_iban(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)

    await process(setup(qr=NO_QR))
    await process(setup(qr=make_invoice(iban="CH6431961000004421558")))

    assert caplog.records, "expected the service to log"
    for record in caplog.records:
        assert QR_IBAN not in str(record.__dict__)
        assert "CH6431961000004421558" not in str(record.__dict__)
