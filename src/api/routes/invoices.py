from http import HTTPStatus
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, File, Query, Response, UploadFile

from src.api.deps import (
    ClientIdDep,
    EnqueueIngestionDep,
    GetInvoiceDep,
    ListInvoicesDep,
    RequestIdDep,
    SettingsDep,
    limit_uploads,
)
from src.api.errors import ApiError, UploadTooLargeError
from src.api.schemas.error import STORAGE_UNAVAILABLE, ErrorResponse
from src.api.schemas.ingestion import IngestionResponse
from src.api.schemas.invoice import InvoicePageResponse, InvoiceResponse
from src.application.errors import UnreadableDocumentError
from src.application.models.ingest_job import JobStatus
from src.application.ports.document_reader import DocumentType
from src.application.ports.invoice_repository import InvoiceQuery
from src.config import Settings
from src.domain.models.amount import Currency

POLL_AFTER_SECONDS = 2
# The first bytes every PDF starts with (its "magic number").
PDF_SIGNATURE = b"%PDF-"
# 21 characters, plus room for the spaces of the printed form ("CH44 3199 9123 0008 8901 2").
IBAN_FILTER_MAX_LENGTH = 34

router = APIRouter(prefix="/invoices", tags=["invoices"])


@router.post(
    "",
    status_code=HTTPStatus.ACCEPTED,
    dependencies=[Depends(limit_uploads)],
    responses={
        200: {"model": IngestionResponse, "description": "Same file, already done: its job"},
        400: {"model": ErrorResponse, "description": "Empty, or content doesn't match the type"},
        413: {"model": ErrorResponse, "description": "File larger than MAX_UPLOAD_MB"},
        415: {"model": ErrorResponse, "description": "Not a PDF"},
        429: {"model": ErrorResponse, "description": "Upload rate exceeded"},
        **STORAGE_UNAVAILABLE,
    },
)
async def ingest_invoice(
    file: Annotated[
        UploadFile,
        File(description="The invoice as `application/pdf`, at most MAX_UPLOAD_MB (default 5 MB)"),
    ],
    service: EnqueueIngestionDep,
    settings: SettingsDep,
    client_id: ClientIdDep,
    request_id: RequestIdDep,
    response: Response,
) -> IngestionResponse:
    """Queue the file for processing: `202` and the job, to poll at `Location`."""
    _check_media_type(file.content_type)
    content = await _read_limited(file, settings)
    check_signature(content)
    job = await service.execute(
        content, DocumentType.PDF, client_id=client_id, request_id=request_id
    )
    response.headers["Location"] = f"/ingestions/{job.id}"
    if job.status is JobStatus.DONE:
        response.status_code = HTTPStatus.OK
    else:
        response.headers["Retry-After"] = str(POLL_AFTER_SECONDS)
    return IngestionResponse.from_job(job)


@router.get(
    "/{invoice_id}",
    responses={
        404: {"model": ErrorResponse, "description": "Invoice not found"},
        **STORAGE_UNAVAILABLE,
    },
)
async def get_invoice(invoice_id: UUID, service: GetInvoiceDep) -> InvoiceResponse:
    return InvoiceResponse.from_domain(await service.execute(invoice_id))


@router.get("", responses=STORAGE_UNAVAILABLE)
async def list_invoices(
    service: ListInvoicesDep,
    creditor_iban: Annotated[str | None, Query(max_length=IBAN_FILTER_MAX_LENGTH)] = None,
    currency: Currency | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> InvoicePageResponse:
    query = InvoiceQuery(creditor_iban=creditor_iban, currency=currency, limit=limit, offset=offset)
    page = await service.execute(query)
    return InvoicePageResponse.from_domain(page, limit=limit, offset=offset)


def _check_media_type(content_type: str | None) -> None:
    media_type = (content_type or "").split(";")[0].strip().lower()
    if media_type != DocumentType.PDF:
        raise ApiError(
            HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
            "unsupported_media_type",
            "File must be application/pdf",
        )


async def _read_limited(file: UploadFile, settings: Settings) -> bytes:
    """The exact file size limit. `UploadLimitMiddleware` already stopped any much larger body
    while it was received; this checks the file without the multipart envelope around it."""
    # Read one byte past the limit: enough to detect an oversized file without loading all of it.
    content = await file.read(settings.max_upload_bytes + 1)
    if len(content) > settings.max_upload_bytes:
        raise UploadTooLargeError(settings.max_upload_mb)
    if not content:
        raise UnreadableDocumentError("File is empty")
    return content


def check_signature(content: bytes) -> None:
    """Reject garbage and mislabelled files at upload, before anything is stored or queued.

    Only the first bytes: whether the file is readable as a whole is the worker's job.
    """
    if not content.startswith(PDF_SIGNATURE):
        raise UnreadableDocumentError("File content is not a valid PDF file")
