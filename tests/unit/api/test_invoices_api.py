import hashlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient, Response

from src.api.schemas.error import ErrorResponse
from src.api.upload_limit import MULTIPART_OVERHEAD_BYTES
from src.application.models.ingest_job import IngestJob, JobError, JobErrorDetail, JobStatus
from src.config import Settings
from src.domain.models.amount import Currency
from src.main import create_app
from tests.unit.application.fakes import (
    FakeRateLimiter,
    FixedClock,
    InMemoryInvoiceRepository,
    InMemoryJobQueue,
    InMemoryUnitOfWork,
)
from tests.unit.application.jobs import make_document, make_job
from tests.unit.context import app_with, make_context
from tests.unit.domain.factories import QR_IBAN, make_invoice

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
PDF = ("invoice.pdf", b"%PDF-1.7 fake", "application/pdf")
PNG = ("invoice.png", b"\x89PNG\r\n\x1a\n fake", "image/png")
JPEG = ("invoice.jpg", b"\xff\xd8\xff\xe0 fake", "image/jpeg")
LIMITS = Settings(uploads_per_client_per_minute=3)


@asynccontextmanager
async def api(
    *,
    uow: InMemoryUnitOfWork | None = None,
    invoices: InMemoryInvoiceRepository | None = None,
    limiter: FakeRateLimiter | None = None,
) -> AsyncIterator[tuple[AsyncClient, InMemoryUnitOfWork]]:
    """The real app and real services, with fakes behind the ports."""
    unit_of_work = uow or InMemoryUnitOfWork(invoices=invoices)
    context = make_context(
        settings=LIMITS,
        unit_of_work=unit_of_work.factory,
        rate_limiter=limiter or FakeRateLimiter(),
        clock=FixedClock(NOW),
    )
    app = app_with(create_app(), context)
    # raise_app_exceptions=False: assert on the 500 response instead of the re-raised error.
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, unit_of_work


async def upload(
    client: AsyncClient, file: tuple[str, bytes, str] = PDF, **kwargs: Any
) -> Response:
    return await client.post("/invoices", files={"file": file}, **kwargs)


def pdf(number: int) -> tuple[str, bytes, str]:
    """A distinct file per number, so the upload isn't recognised as a re-upload."""
    return (f"invoice-{number}.pdf", f"%PDF-1.7 fake {number}".encode(), "application/pdf")


def error_of(response: Response) -> dict[str, Any]:
    """Asserts the shared error shape and returns its `error` object."""
    ErrorResponse.model_validate(response.json())
    error: dict[str, Any] = response.json()["error"]
    return error


def stored_job_for(file_content: bytes, **changes: Any) -> tuple[InMemoryUnitOfWork, IngestJob]:
    """A unit of work that already holds a job for `file_content`."""
    document = make_document(content=file_content)
    job = make_job(document, **changes)
    uow = InMemoryUnitOfWork()
    uow.documents.documents[document.id] = document
    uow.jobs.jobs[job.id] = job
    return uow, job


# --- POST /invoices: a new file ------------------------------------------------------------------


async def test_upload_stores_the_file_and_queues_a_job() -> None:
    async with api() as (client, uow):
        response = await upload(client, headers={"X-Request-ID": "upload-1"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert (body["attempts"], body["invoice_id"], body["error"]) == (0, None, None)
    assert response.headers["location"] == f"/ingestions/{body['id']}"
    assert response.headers["retry-after"] == "2"
    [job] = uow.jobs.jobs.values()
    [document] = uow.documents.documents.values()
    assert job.document_id == document.id
    assert document.content == PDF[1]
    assert job.content_sha256 == hashlib.sha256(PDF[1]).hexdigest()
    assert (job.client_id, job.request_id) == ("127.0.0.1", "upload-1")
    assert uow.commits == 1  # document and job in one transaction


async def test_the_client_ip_is_never_returned() -> None:
    async with api() as (client, _):
        response = await upload(client)

    assert "127.0.0.1" not in response.text
    assert set(response.json()) == {
        "id",
        "status",
        "attempts",
        "invoice_id",
        "error",
        "created_at",
        "updated_at",
    }


async def test_accepts_a_pdf() -> None:
    async with api() as (client, _):
        response = await upload(client, PDF)

    assert response.status_code == 202


@pytest.mark.parametrize(
    "file",
    [
        pytest.param(PNG, id="PNG"),
        pytest.param(JPEG, id="JPEG"),
        pytest.param(("a.pdf", PDF[1], "image/png"), id="PDF declared as PNG"),
        pytest.param(("invoice.txt", b"hello", "text/plain"), id="text"),
        pytest.param(("invoice", PDF[1], "application/octet-stream"), id="no specific type"),
    ],
)
async def test_anything_but_a_pdf_is_415_and_not_stored(file: tuple[str, bytes, str]) -> None:
    async with api() as (client, uow):
        response = await upload(client, file)

    assert response.status_code == 415
    assert error_of(response) == {
        "code": "unsupported_media_type",
        "message": "File must be application/pdf",
        "details": [],
    }
    assert uow.jobs.jobs == {}
    assert uow.documents.documents == {}


@pytest.mark.parametrize(
    "file",
    [
        pytest.param(("a.pdf", PNG[1], "application/pdf"), id="PNG declared as PDF"),
        pytest.param(("a.pdf", JPEG[1], "application/pdf"), id="JPEG declared as PDF"),
        pytest.param(("a.pdf", b"PDF-1.7 almost", "application/pdf"), id="garbage"),
    ],
)
async def test_content_that_isnt_a_pdf_is_400_and_not_stored(
    file: tuple[str, bytes, str],
) -> None:
    async with api() as (client, uow):
        response = await upload(client, file)

    assert response.status_code == 400
    assert error_of(response)["code"] == "unreadable_document"
    assert uow.jobs.jobs == {}
    assert uow.documents.documents == {}


async def test_empty_file_is_400() -> None:
    async with api() as (client, uow):
        response = await upload(client, ("empty.pdf", b"", "application/pdf"))

    assert response.status_code == 400
    assert error_of(response) == {
        "code": "unreadable_document",
        "message": "File is empty",
        "details": [],
    }
    assert uow.jobs.jobs == {}


def pdf_of_size(size: int) -> tuple[str, bytes, str]:
    return ("sized.pdf", b"%PDF-" + b"0" * (size - 5), "application/pdf")


async def test_pdf_over_the_size_limit_is_413() -> None:
    """Just over: within the multipart allowance of the body limit, so the route's exact check."""
    async with api() as (client, uow):
        response = await upload(client, pdf_of_size(LIMITS.max_upload_bytes + 1))

    assert response.status_code == 413
    assert error_of(response) == {
        "code": "file_too_large",
        "message": f"File must be at most {LIMITS.max_upload_mb} MB",
        "details": [],
    }
    assert uow.jobs.jobs == {}


@pytest.mark.parametrize(
    "size",
    [LIMITS.max_upload_bytes, LIMITS.max_upload_bytes - 1],
    ids=["exactly the limit", "just under"],
)
async def test_pdf_up_to_the_size_limit_is_accepted(size: int) -> None:
    async with api() as (client, _):
        response = await upload(client, pdf_of_size(size))

    assert response.status_code == 202


async def test_far_too_large_body_is_413_from_its_content_length() -> None:
    body_limit = LIMITS.max_upload_bytes + MULTIPART_OVERHEAD_BYTES
    async with api() as (client, uow):
        response = await upload(client, pdf_of_size(body_limit + 1))

    assert response.status_code == 413
    assert error_of(response)["code"] == "file_too_large"
    assert uow.jobs.jobs == {}


async def test_body_without_content_length_is_cut_off_at_the_limit() -> None:
    """Chunked transfer: no length up front, so the limit applies to the bytes as they arrive."""
    boundary = "limit-test"
    chunk = b"0" * 1024 * 1024
    chunks_sent = 0

    async def chunked_body() -> AsyncIterator[bytes]:
        nonlocal chunks_sent
        head = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="big.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n%PDF-"
        )
        yield head.encode()
        for _ in range(100):  # 100 MB if nobody stops reading
            chunks_sent += 1
            yield chunk
        yield f"\r\n--{boundary}--\r\n".encode()

    async with api() as (client, uow):
        response = await client.post(
            "/invoices",
            content=chunked_body(),
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        )

    assert response.status_code == 413
    assert error_of(response)["code"] == "file_too_large"
    assert chunks_sent <= LIMITS.max_upload_mb + 1
    assert uow.jobs.jobs == {}


async def test_missing_file_field_is_a_request_validation_error() -> None:
    async with api() as (client, _):
        response = await client.post("/invoices", data={"other": "x"})

    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "request_validation"
    assert error["details"][0]["field"] == "body.file"


class BrokenJobRepository(InMemoryJobQueue):
    async def get_by_content_sha256(self, content_sha256: str) -> IngestJob | None:
        raise RuntimeError("connection string with secret password")


async def test_unexpected_error_is_500_without_internals() -> None:
    async with api(uow=InMemoryUnitOfWork(jobs=BrokenJobRepository())) as (client, _):
        response = await upload(client)

    assert response.status_code == 500
    assert error_of(response) == {
        "code": "internal_error",
        "message": "An unexpected error occurred",
        "details": [],
    }
    assert "password" not in response.text


# --- POST /invoices: the same file again ---------------------------------------------------------

LOCKED = {"locked_by": "worker-1", "locked_until": NOW}


@pytest.mark.parametrize(
    "changes",
    [{"status": JobStatus.PENDING}, {"status": JobStatus.PROCESSING, **LOCKED}],
    ids=["pending", "processing"],
)
async def test_reupload_of_a_queued_file_returns_its_job(changes: dict[str, Any]) -> None:
    uow, job = stored_job_for(PDF[1], **changes)
    async with api(uow=uow) as (client, _):
        response = await upload(client)

    assert response.status_code == 202
    assert response.json()["id"] == str(job.id)
    assert response.headers["retry-after"] == "2"
    assert len(uow.jobs.jobs) == 1
    assert len(uow.documents.documents) == 1  # not stored twice


async def test_reupload_of_a_done_file_is_200_with_its_invoice() -> None:
    invoice_id = uuid4()
    uow, job = stored_job_for(PDF[1], status=JobStatus.DONE, attempts=1, invoice_id=invoice_id)
    async with api(uow=uow) as (client, _):
        response = await upload(client)

    assert response.status_code == 200
    assert response.json()["invoice_id"] == str(invoice_id)
    assert response.headers["location"] == f"/ingestions/{job.id}"
    assert "retry-after" not in response.headers


async def test_reupload_of_a_failed_file_queues_it_again() -> None:
    error = JobError(code="internal_error", message="An unexpected error occurred")
    uow, job = stored_job_for(PDF[1], status=JobStatus.FAILED, attempts=3, error=error)
    async with api(uow=uow) as (client, _):
        response = await upload(client, headers={"X-Request-ID": "retry-upload"})

    assert response.status_code == 202
    body = response.json()
    assert (body["id"], body["status"], body["attempts"], body["error"]) == (
        str(job.id),
        "pending",
        0,
        None,
    )
    assert uow.jobs.jobs[job.id].request_id == "retry-upload"


# --- Rate limits ---------------------------------------------------------------------------------


async def test_uploads_over_the_client_limit_are_429_with_retry_after() -> None:
    async with api() as (client, _):
        for number in range(LIMITS.uploads_per_client_per_minute):
            assert (await upload(client, pdf(number))).status_code == 202
        response = await upload(client, pdf(99))

    assert response.status_code == 429
    assert error_of(response)["code"] == "rate_limited"
    assert response.headers["retry-after"] == str(FakeRateLimiter.RETRY_AFTER_SECONDS)


async def test_uploads_still_work_when_the_limiter_is_down() -> None:
    async with api(limiter=FakeRateLimiter(unavailable=True)) as (client, _):
        response = await upload(client)

    assert response.status_code == 202


# --- GET /ingestions/{id} ------------------------------------------------------------------------


async def test_get_ingestion_returns_the_job() -> None:
    uow, job = stored_job_for(PDF[1])
    async with api(uow=uow) as (client, _):
        response = await client.get(f"/ingestions/{job.id}")

    assert response.status_code == 200
    assert response.json()["status"] == "pending"


async def test_failed_ingestion_carries_the_error_in_the_error_shape() -> None:
    error = JobError(
        code="invalid_invoice",
        message="Invoice failed validation with 1 error(s)",
        details=(
            JobErrorDetail(
                code="invalid_iban",
                field="creditor.iban",
                message="IBAN check digits are wrong",
                severity="error",
            ),
        ),
    )
    uow, job = stored_job_for(PDF[1], status=JobStatus.FAILED, attempts=1, error=error)
    async with api(uow=uow) as (client, _):
        response = await client.get(f"/ingestions/{job.id}")

    assert response.json()["error"] == {
        "code": "invalid_invoice",
        "message": "Invoice failed validation with 1 error(s)",
        "details": [
            {
                "code": "invalid_iban",
                "field": "creditor.iban",
                "message": "IBAN check digits are wrong",
                "severity": "error",
            }
        ],
    }


async def test_unknown_ingestion_is_404() -> None:
    missing = uuid4()
    async with api() as (client, _):
        response = await client.get(f"/ingestions/{missing}")

    assert response.status_code == 404
    assert error_of(response) == {
        "code": "ingestion_not_found",
        "message": f"Ingestion {missing} not found",
        "details": [],
    }


async def test_ingestion_with_malformed_id_is_422() -> None:
    async with api() as (client, _):
        response = await client.get("/ingestions/not-a-uuid")

    assert response.status_code == 422


# --- GET /invoices/{id} --------------------------------------------------------------------------


async def test_get_returns_the_invoice() -> None:
    invoice = make_invoice()
    async with api(invoices=InMemoryInvoiceRepository((invoice,))) as (client, _):
        response = await client.get(f"/invoices/{invoice.id}")

    assert response.status_code == 200
    assert response.json()["id"] == str(invoice.id)


async def test_get_unknown_invoice_is_404() -> None:
    missing = uuid4()
    async with api() as (client, _):
        response = await client.get(f"/invoices/{missing}")

    assert response.status_code == 404
    assert error_of(response) == {
        "code": "invoice_not_found",
        "message": f"Invoice {missing} not found",
        "details": [],
    }


async def test_get_with_malformed_id_is_422() -> None:
    async with api() as (client, _):
        response = await client.get("/invoices/not-a-uuid")

    assert response.status_code == 422
    assert error_of(response)["details"][0]["field"] == "path.invoice_id"


# --- GET /invoices -------------------------------------------------------------------------------


async def test_list_returns_a_page_with_paging_info() -> None:
    invoices = tuple(
        replace(make_invoice(), id=uuid4(), created_at=NOW.replace(minute=minute))
        for minute in range(3)
    )
    async with api(invoices=InMemoryInvoiceRepository(invoices)) as (client, _):
        response = await client.get("/invoices", params={"limit": 2, "offset": 0})

    body = response.json()
    assert response.status_code == 200
    assert [item["id"] for item in body["items"]] == [str(invoices[2].id), str(invoices[1].id)]
    assert (body["total"], body["limit"], body["offset"]) == (3, 2, 0)


async def test_list_filters_by_currency_and_creditor_iban() -> None:
    chf = make_invoice()
    eur = make_invoice(currency=Currency.EUR)
    async with api(invoices=InMemoryInvoiceRepository((chf, eur))) as (client, _):
        response = await client.get(
            "/invoices", params={"currency": "EUR", "creditor_iban": QR_IBAN}
        )

    assert [item["id"] for item in response.json()["items"]] == [str(eur.id)]


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"limit": 0}, "query.limit"),
        ({"limit": 101}, "query.limit"),
        ({"offset": -1}, "query.offset"),
        ({"currency": "USD"}, "query.currency"),
        ({"creditor_iban": "C" * 22}, "query.creditor_iban"),
    ],
)
async def test_list_rejects_invalid_query_parameters(params: dict[str, Any], field: str) -> None:
    async with api() as (client, _):
        response = await client.get("/invoices", params=params)

    assert response.status_code == 422
    assert error_of(response)["details"][0]["field"] == field


# --- Framework errors use the same shape ---------------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path", "status", "code"),
    [
        ("GET", "/nope", 404, "not_found"),
        ("DELETE", "/invoices", 405, "method_not_allowed"),
    ],
)
async def test_framework_errors_use_the_error_shape(
    method: str, path: str, status: int, code: str
) -> None:
    async with api() as (client, _):
        response = await client.request(method, path)

    assert response.status_code == status
    assert error_of(response)["code"] == code
