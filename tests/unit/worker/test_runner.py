import asyncio
import random
from datetime import UTC, datetime
from uuid import UUID

from src.application.llm_quota import LlmQuota
from src.application.models.ingest_job import IngestJob, JobStatus
from src.application.ports.document_reader import Document
from src.application.retry_policy import RetryPolicy
from src.application.services.process_ingest_job import ProcessIngestJob
from src.worker.runner import JobProcessor, Worker
from tests.unit.application.fakes import (
    FakeDocumentReader,
    FakeInvoiceParser,
    FakeRateLimiter,
    FixedClock,
    InMemoryExtractionFailureCache,
    InMemoryJobQueue,
    InMemoryUnitOfWork,
)
from tests.unit.application.jobs import make_document, make_job
from tests.unit.domain.factories import make_invoice

WORKER = "worker-1"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def queued(uow: InMemoryUnitOfWork, count: int = 1) -> list[IngestJob]:
    jobs = []
    for number in range(count):
        document = make_document(content=f"%PDF- {number}".encode())
        job = make_job(document)
        uow.documents.documents[document.id] = document
        uow.jobs.jobs[job.id] = job
        jobs.append(job)
    return jobs


def worker(
    uow: InMemoryUnitOfWork, process: JobProcessor, *, shutdown_timeout: float = 5
) -> Worker:
    return Worker(
        worker_id=WORKER,
        unit_of_work=uow.factory,
        process=process,
        lease_seconds=300,
        max_attempts=5,
        concurrency=2,
        poll_interval=0.01,
        shutdown_timeout=shutdown_timeout,
    )


class Gate:
    """A job processor that waits until opened, to hold jobs mid-processing."""

    def __init__(self) -> None:
        self.entered: list[UUID] = []
        self.opened = asyncio.Event()
        self._changed = asyncio.Condition()

    async def __call__(self, job: IngestJob) -> None:
        async with self._changed:
            self.entered.append(job.id)
            self._changed.notify_all()
        await self.opened.wait()

    async def until_entered(self, count: int) -> None:
        async with self._changed:
            await self._changed.wait_for(lambda: len(self.entered) >= count)


async def test_stop_lets_in_flight_jobs_finish_and_claims_no_more() -> None:
    uow = InMemoryUnitOfWork()
    first, second, third = queued(uow, 3)
    gate = Gate()
    stop = asyncio.Event()
    running = asyncio.create_task(worker(uow, gate).run(stop))
    await gate.until_entered(2)  # concurrency 2: the third waits

    stop.set()
    await asyncio.sleep(0.05)
    gate.opened.set()
    await asyncio.wait_for(running, timeout=1)

    assert gate.entered == [first.id, second.id]
    assert uow.jobs.jobs[third.id].status is JobStatus.PENDING  # never claimed


async def test_jobs_still_running_at_the_timeout_are_released_with_their_attempt_back() -> None:
    uow = InMemoryUnitOfWork()
    [job] = queued(uow)
    gate = Gate()  # never opened: the job overruns the shutdown timeout
    stop = asyncio.Event()
    running = asyncio.create_task(worker(uow, gate, shutdown_timeout=0.05).run(stop))
    await gate.until_entered(1)
    assert uow.jobs.jobs[job.id].attempts == 1

    stop.set()
    await asyncio.wait_for(running, timeout=1)

    released = uow.jobs.jobs[job.id]
    assert (released.status, released.attempts, released.locked_by) == (JobStatus.PENDING, 0, None)


class SlowCompletion(InMemoryJobQueue):
    """`complete` (inside the finishing transaction) waits until allowed to go on."""

    def __init__(self) -> None:
        super().__init__()
        self.completing = asyncio.Event()
        self.may_complete = asyncio.Event()

    async def complete(self, job_id: UUID, *, worker_id: str, invoice_id: UUID) -> bool:
        self.completing.set()
        await self.may_complete.wait()
        return await super().complete(job_id, worker_id=worker_id, invoice_id=invoice_id)


async def test_shutdown_during_the_commit_still_ends_consistent() -> None:
    jobs = SlowCompletion()
    uow = InMemoryUnitOfWork(jobs=jobs)
    [job] = queued(uow)
    process = ProcessIngestJob(
        worker_id=WORKER,
        document_reader=FakeDocumentReader(Document(pages=(b"p.png",), text="")),
        qr_parser=FakeInvoiceParser(make_invoice()),
        llm_parser=FakeInvoiceParser(make_invoice()),
        llm_quota=LlmQuota(FakeRateLimiter(), per_client_per_hour=1, per_day=1),
        llm_failure_cache=InMemoryExtractionFailureCache(),
        unit_of_work=uow.factory,
        clock=FixedClock(NOW),
        retry_policy=RetryPolicy(
            max_attempts=5, base_seconds=1, cap_seconds=1, rng=random.Random(1)
        ),
    )
    stop = asyncio.Event()
    running = asyncio.create_task(worker(uow, process.execute, shutdown_timeout=0.01).run(stop))
    await jobs.completing.wait()

    stop.set()  # the timeout runs out while the commit is in progress: the task is cancelled
    await asyncio.sleep(0.05)
    jobs.may_complete.set()
    await asyncio.wait_for(running, timeout=1)

    done = uow.jobs.jobs[job.id]
    assert done.status is JobStatus.DONE  # not released halfway
    assert done.invoice_id in uow.invoices.invoices  # job and invoice, together
