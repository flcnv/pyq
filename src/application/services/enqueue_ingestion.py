import hashlib
import logging
from uuid import uuid4

from src.application.errors import JobAlreadyExistsError
from src.application.models.ingest_job import IngestJob, JobStatus
from src.application.ports.clock import Clock
from src.application.ports.document_reader import DocumentType
from src.application.ports.document_storage import StoredDocument
from src.application.ports.unit_of_work import UnitOfWorkFactory

logger = logging.getLogger(__name__)


class EnqueueIngestion:
    """Store an upload and queue it for the worker, in one transaction: there is never a job
    without its document, nor a stored document nobody will process.

    One job per file (by SHA-256): a re-upload returns the existing job; a `failed` one is queued
    again, since the cause (a bug, the budget, an outage) may be gone.
    """

    def __init__(self, *, unit_of_work: UnitOfWorkFactory, clock: Clock) -> None:
        self._unit_of_work = unit_of_work
        self._clock = clock

    async def execute(
        self,
        content: bytes,
        document_type: DocumentType,
        *,
        client_id: str,
        request_id: str | None,
    ) -> IngestJob:
        content_sha256 = hashlib.sha256(content).hexdigest()
        async with self._unit_of_work() as uow:
            existing = await uow.jobs.get_by_content_sha256(content_sha256)
        if existing is not None:
            return await self._existing(existing, client_id=client_id, request_id=request_id)

        document = StoredDocument(
            id=uuid4(),
            content=content,
            content_type=document_type,
            size=len(content),
            sha256=content_sha256,
            created_at=self._clock.now(),
        )
        try:
            async with self._unit_of_work() as uow:
                await uow.documents.add(document)
                job = await uow.jobs.add(self._new_job(document, client_id, request_id))
                await uow.commit()
        except JobAlreadyExistsError:
            # The same file was enqueued by a concurrent upload; ours was rolled back.
            async with self._unit_of_work() as uow:
                raced = await uow.jobs.get_by_content_sha256(content_sha256)
            if raced is None:
                raise
            return await self._existing(raced, client_id=client_id, request_id=request_id)
        logger.info("ingestion queued", extra={"job_id": str(job.id), "size": document.size})
        return job

    def _new_job(
        self, document: StoredDocument, client_id: str, request_id: str | None
    ) -> IngestJob:
        now = self._clock.now()  # placeholder: the database sets the job's timestamps
        return IngestJob(
            id=uuid4(),
            document_id=document.id,
            content_sha256=document.sha256,
            status=JobStatus.PENDING,
            attempts=0,
            run_after=now,
            locked_by=None,
            locked_until=None,
            error=None,
            invoice_id=None,
            client_id=client_id,
            request_id=request_id,
            created_at=now,
            updated_at=now,
        )

    async def _existing(
        self, job: IngestJob, *, client_id: str, request_id: str | None
    ) -> IngestJob:
        if job.status is not JobStatus.FAILED:
            return job
        async with self._unit_of_work() as uow:
            requeued = await uow.jobs.reset_failed(
                job.id, client_id=client_id, request_id=request_id
            )
            await uow.commit()
        if requeued is not None:
            logger.info("failed ingestion queued again", extra={"job_id": str(job.id)})
            return requeued
        async with self._unit_of_work() as uow:  # another upload requeued it first
            current = await uow.jobs.get(job.id)
        return current or job
