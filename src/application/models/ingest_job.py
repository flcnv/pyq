from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID


class JobStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass(frozen=True, slots=True, kw_only=True)
class JobErrorDetail:
    code: str
    message: str
    field: str | None = None
    severity: str | None = None


@dataclass(frozen=True, slots=True, kw_only=True)
class JobError:
    """Why a job failed, in the same shape as the API's error body."""

    code: str
    message: str
    details: tuple[JobErrorDetail, ...] = ()


@dataclass(frozen=True, slots=True, kw_only=True)
class IngestJob:
    """One uploaded file on its way to becoming an invoice. Not an invoice: that exists only
    after successful processing (`invoice_id`)."""

    id: UUID
    document_id: UUID | None  # None for jobs migrated from before documents were stored
    content_sha256: str
    status: JobStatus
    attempts: int
    run_after: datetime
    locked_by: str | None
    locked_until: datetime | None
    error: JobError | None
    invoice_id: UUID | None
    client_id: str | None
    request_id: str | None
    created_at: datetime
    updated_at: datetime
