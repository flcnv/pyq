from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from src.api.schemas.error import ErrorBody
from src.application.models.ingest_job import IngestJob, JobStatus


class IngestionResponse(BaseModel):
    """An upload's progress. No `client_id` (an IP address) and no lock fields: internal only."""

    id: UUID
    status: JobStatus
    attempts: int
    invoice_id: UUID | None
    error: ErrorBody | None  # set when `failed`: the same code, message and details as an error
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_job(cls, job: IngestJob) -> IngestionResponse:
        return cls(
            id=job.id,
            status=job.status,
            attempts=job.attempts,
            invoice_id=job.invoice_id,
            error=ErrorBody.from_job_error(job.error) if job.error else None,
            created_at=job.created_at,
            updated_at=job.updated_at,
        )
