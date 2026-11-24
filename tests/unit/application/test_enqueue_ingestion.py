import hashlib
from datetime import UTC, datetime
from typing import Any

import pytest

from src.application.models.ingest_job import IngestJob, JobError, JobStatus
from src.application.ports.document_reader import DocumentType
from src.application.services.enqueue_ingestion import EnqueueIngestion
from tests.unit.application.fakes import (
    FixedClock,
    InMemoryJobQueue,
    InMemoryUnitOfWork,
)
from tests.unit.application.jobs import make_document, make_job

NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
CONTENT = b"%PDF-1.7 invoice"
SHA256 = hashlib.sha256(CONTENT).hexdigest()
FAILED = JobError(code="internal_error", message="An unexpected error occurred")


def service(uow: InMemoryUnitOfWork) -> EnqueueIngestion:
    return EnqueueIngestion(unit_of_work=uow.factory, clock=FixedClock(NOW))


async def enqueue(
    uow: InMemoryUnitOfWork, client_id: str = "c1", request_id: str = "r1"
) -> IngestJob:
    return await service(uow).execute(
        CONTENT, DocumentType.PDF, client_id=client_id, request_id=request_id
    )


def with_job(**changes: Any) -> tuple[InMemoryUnitOfWork, IngestJob]:
    document = make_document(content=CONTENT)
    job = make_job(document, **changes)
    uow = InMemoryUnitOfWork()
    uow.documents.documents[document.id] = document
    uow.jobs.jobs[job.id] = job
    return uow, job


async def test_new_file_is_stored_with_a_pending_job_in_one_commit() -> None:
    uow = InMemoryUnitOfWork()

    job = await enqueue(uow)

    assert (job.status, job.attempts, job.content_sha256) == (JobStatus.PENDING, 0, SHA256)
    assert (job.client_id, job.request_id) == ("c1", "r1")
    document = uow.documents.documents[job.document_id]  # type: ignore[index]
    assert (document.content, document.size, document.sha256) == (CONTENT, len(CONTENT), SHA256)
    assert uow.commits == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"status": JobStatus.PENDING},
        {"status": JobStatus.PROCESSING, "locked_by": "w1", "locked_until": NOW},
        {"status": JobStatus.DONE, "invoice_id": make_job().id},
    ],
    ids=["pending", "processing", "done"],
)
async def test_known_file_returns_its_job_unchanged(changes: dict[str, Any]) -> None:
    uow, job = with_job(**changes)

    assert await enqueue(uow) == job
    assert len(uow.documents.documents) == 1
    assert uow.commits == 0


async def test_failed_file_is_queued_again_for_the_new_uploader() -> None:
    uow, job = with_job(status=JobStatus.FAILED, attempts=5, error=FAILED)

    requeued = await enqueue(uow, client_id="c2", request_id="r2")

    assert requeued.id == job.id
    assert (requeued.status, requeued.attempts, requeued.error) == (JobStatus.PENDING, 0, None)
    assert (requeued.client_id, requeued.request_id) == ("c2", "r2")


class RacingJobs(InMemoryJobQueue):
    """Another upload of the same file lands between our lookup and our insert."""

    def __init__(self, racing_job: IngestJob) -> None:
        super().__init__()
        self._racing_job = racing_job
        self.lookups = 0

    async def get_by_content_sha256(self, content_sha256: str) -> IngestJob | None:
        self.lookups += 1
        if self.lookups == 1:
            self.jobs[self._racing_job.id] = self._racing_job  # it lands right now
            return None
        return await super().get_by_content_sha256(content_sha256)


async def test_concurrent_upload_of_the_same_file_returns_the_winners_job() -> None:
    winner = make_job(make_document(content=CONTENT))
    uow = InMemoryUnitOfWork(jobs=RacingJobs(winner))

    job = await enqueue(uow)

    assert job == winner
    assert list(uow.jobs.jobs) == [winner.id]


class ResetByOthers(InMemoryJobQueue):
    """Another re-upload requeues the failed job just before we try to."""

    async def reset_failed(
        self, job_id: Any, *, client_id: str, request_id: str | None
    ) -> IngestJob | None:
        await super().reset_failed(job_id, client_id="other", request_id="other-request")
        return None


async def test_concurrent_requeue_returns_the_job_as_requeued_by_the_other_upload() -> None:
    document = make_document(content=CONTENT)
    failed = make_job(document, status=JobStatus.FAILED, error=FAILED)
    jobs = ResetByOthers()
    jobs.jobs[failed.id] = failed

    job = await enqueue(InMemoryUnitOfWork(jobs=jobs))

    assert (job.status, job.client_id) == (JobStatus.PENDING, "other")
