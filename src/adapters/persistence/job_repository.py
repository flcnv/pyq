from datetime import timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.orm.ingest_job_record import IngestJobRecord
from src.application.errors import JobAlreadyExistsError
from src.application.models.ingest_job import IngestJob, JobError, JobErrorDetail, JobStatus

UNIQUE_CONTENT_SHA256 = "uq_ingest_jobs_content_sha256"


class SqlAlchemyIngestJobRepository:
    """`IngestJobRepository` on the unit of work's session: it never commits by itself."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, job: IngestJob) -> IngestJob:
        record = _to_record(job)
        record.run_after = record.created_at = record.updated_at = func.now()
        try:
            async with self._session.begin_nested():
                self._session.add(record)
        except IntegrityError as error:
            if _violated_constraint(error) == UNIQUE_CONTENT_SHA256:
                raise JobAlreadyExistsError("A job for this file already exists") from error
            raise
        await self._session.refresh(record)
        return _to_model(record)

    async def get(self, job_id: UUID) -> IngestJob | None:
        record = await self._session.get(IngestJobRecord, job_id)
        return None if record is None else _to_model(record)

    async def get_by_content_sha256(self, content_sha256: str) -> IngestJob | None:
        statement = select(IngestJobRecord).where(IngestJobRecord.content_sha256 == content_sha256)
        record = await self._session.scalar(statement)
        return None if record is None else _to_model(record)

    async def reset_failed(
        self, job_id: UUID, *, client_id: str, request_id: str | None
    ) -> IngestJob | None:
        statement = (
            update(IngestJobRecord)
            .where(IngestJobRecord.id == job_id, IngestJobRecord.status == JobStatus.FAILED)
            .values(
                status=JobStatus.PENDING,
                attempts=0,
                run_after=func.now(),
                error_code=None,
                error_message=None,
                error_details=None,
                client_id=client_id,
                request_id=request_id,
                updated_at=func.now(),
            )
            .returning(IngestJobRecord)
        )
        record = await self._session.scalar(statement)
        return None if record is None else _to_model(record)

    async def claim(
        self, *, worker_id: str, lease_seconds: int, max_attempts: int
    ) -> IngestJob | None:
        due = (
            select(IngestJobRecord.id)
            .where(
                or_(
                    (IngestJobRecord.status == JobStatus.PENDING)
                    & (IngestJobRecord.run_after <= func.now()),
                    (IngestJobRecord.status == JobStatus.PROCESSING)
                    & (IngestJobRecord.locked_until < func.now())
                    & (IngestJobRecord.attempts < max_attempts),
                )
            )
            .order_by(IngestJobRecord.run_after, IngestJobRecord.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(IngestJobRecord)
            .where(IngestJobRecord.id == due)
            .values(
                status=JobStatus.PROCESSING,
                locked_by=worker_id,
                locked_until=func.now() + timedelta(seconds=lease_seconds),
                attempts=IngestJobRecord.attempts + 1,
                updated_at=func.now(),
            )
            .returning(IngestJobRecord)
        )
        record = await self._session.scalar(statement)
        return None if record is None else _to_model(record)

    async def fail_expired_at_max(self, *, max_attempts: int, error: JobError) -> int:
        statement = (
            update(IngestJobRecord)
            .where(
                IngestJobRecord.status == JobStatus.PROCESSING,
                IngestJobRecord.locked_until < func.now(),
                IngestJobRecord.attempts >= max_attempts,
            )
            .values(
                status=JobStatus.FAILED,
                locked_by=None,
                locked_until=None,
                error_code=error.code,
                error_message=error.message,
                error_details=_details_to_json(error),
                updated_at=func.now(),
            )
            .returning(IngestJobRecord.id)
        )
        return len((await self._session.scalars(statement)).all())

    async def retry_later(self, job_id: UUID, *, worker_id: str, delay_seconds: float) -> bool:
        statement = (
            update(IngestJobRecord)
            .where(*_owned_by(job_id, worker_id))
            .values(
                status=JobStatus.PENDING,
                run_after=func.now() + timedelta(seconds=delay_seconds),
                locked_by=None,
                locked_until=None,
                updated_at=func.now(),
            )
            .returning(IngestJobRecord.id)
        )
        return await self._session.scalar(statement) is not None

    async def release(self, job_id: UUID, *, worker_id: str) -> bool:
        statement = (
            update(IngestJobRecord)
            .where(*_owned_by(job_id, worker_id))
            .values(
                status=JobStatus.PENDING,
                attempts=func.greatest(IngestJobRecord.attempts - 1, 0),
                run_after=func.now(),
                locked_by=None,
                locked_until=None,
                updated_at=func.now(),
            )
            .returning(IngestJobRecord.id)
        )
        return await self._session.scalar(statement) is not None

    async def complete(self, job_id: UUID, *, worker_id: str, invoice_id: UUID) -> bool:
        return await self._finish(
            job_id, worker_id, status=JobStatus.DONE, invoice_id=invoice_id, error=None
        )

    async def fail(self, job_id: UUID, *, worker_id: str, error: JobError) -> bool:
        return await self._finish(
            job_id, worker_id, status=JobStatus.FAILED, invoice_id=None, error=error
        )

    async def _finish(
        self,
        job_id: UUID,
        worker_id: str,
        *,
        status: JobStatus,
        invoice_id: UUID | None,
        error: JobError | None,
    ) -> bool:
        statement = (
            update(IngestJobRecord)
            .where(*_owned_by(job_id, worker_id))
            .values(
                status=status,
                invoice_id=invoice_id,
                locked_by=None,
                locked_until=None,
                error_code=error.code if error else None,
                error_message=error.message if error else None,
                error_details=_details_to_json(error) if error else None,
                updated_at=func.now(),
            )
            .returning(IngestJobRecord.id)
        )
        return await self._session.scalar(statement) is not None


def _owned_by(job_id: UUID, worker_id: str) -> tuple[Any, ...]:
    """The fence: still `processing`, and still ours (Any: SQLAlchemy column expressions)."""
    return (
        IngestJobRecord.id == job_id,
        IngestJobRecord.status == JobStatus.PROCESSING,
        IngestJobRecord.locked_by == worker_id,
    )


def _violated_constraint(error: IntegrityError) -> str | None:
    cause = error.orig.__cause__ if error.orig is not None else None
    constraint: str | None = getattr(cause, "constraint_name", None)
    return constraint


# --- Mapping -------------------------------------------------------------------------------------


def _to_record(job: IngestJob) -> IngestJobRecord:
    return IngestJobRecord(
        id=job.id,
        document_id=job.document_id,
        content_sha256=job.content_sha256,
        status=job.status,
        attempts=job.attempts,
        run_after=job.run_after,
        locked_by=job.locked_by,
        locked_until=job.locked_until,
        error_code=job.error.code if job.error else None,
        error_message=job.error.message if job.error else None,
        error_details=_details_to_json(job.error) if job.error else None,
        invoice_id=job.invoice_id,
        client_id=job.client_id,
        request_id=job.request_id,
        created_at=job.created_at,
        updated_at=job.updated_at,
    )


def _to_model(record: IngestJobRecord) -> IngestJob:
    return IngestJob(
        id=record.id,
        document_id=record.document_id,
        content_sha256=record.content_sha256,
        status=record.status,
        attempts=record.attempts,
        run_after=record.run_after,
        locked_by=record.locked_by,
        locked_until=record.locked_until,
        error=_error_from_record(record),
        invoice_id=record.invoice_id,
        client_id=record.client_id,
        request_id=record.request_id,
        created_at=record.created_at,
        updated_at=record.updated_at,
    )


def _error_from_record(record: IngestJobRecord) -> JobError | None:
    if record.error_code is None:
        return None
    return JobError(
        code=record.error_code,
        message=record.error_message or "",
        details=tuple(_detail_from_json(detail) for detail in record.error_details or []),
    )


# Any: JSON objects written and read only by this module.
def _details_to_json(error: JobError) -> list[dict[str, Any]]:
    return [
        {
            "code": detail.code,
            "message": detail.message,
            "field": detail.field,
            "severity": detail.severity,
        }
        for detail in error.details
    ]


def _detail_from_json(data: dict[str, Any]) -> JobErrorDetail:
    return JobErrorDetail(
        code=data["code"],
        message=data["message"],
        field=data.get("field"),
        severity=data.get("severity"),
    )
