import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from src.application.job_errors import LEASE_EXPIRED, retries_exhausted
from src.application.models.ingest_job import IngestJob
from src.application.ports.unit_of_work import UnitOfWorkFactory
from src.logging_setup import job_id_var, request_id_var

logger = logging.getLogger(__name__)

type JobProcessor = Callable[[IngestJob], Awaitable[object]]


class Worker:
    """Claims due jobs and processes up to `concurrency` of them at once, as asyncio tasks.

    Polls every `poll_interval` seconds while there's nothing to do. Every job is claimed in its
    own short transaction; processing happens outside it, so a slow LLM call holds no lock.
    """

    def __init__(
        self,
        *,
        worker_id: str,
        unit_of_work: UnitOfWorkFactory,
        process: JobProcessor,
        lease_seconds: int,
        max_attempts: int,
        concurrency: int,
        poll_interval: float,
        shutdown_timeout: float,
    ) -> None:
        self.worker_id = worker_id
        self._unit_of_work = unit_of_work
        self._process = process
        self._lease_seconds = lease_seconds
        self._max_attempts = max_attempts
        self._concurrency = concurrency
        self._poll_interval = poll_interval
        self._shutdown_timeout = shutdown_timeout
        self._in_flight: set[asyncio.Task[None]] = set()

    async def run(self, stop: asyncio.Event) -> None:
        logger.info("worker started", extra={"worker_id": self.worker_id})
        while not stop.is_set():
            await self._fill_free_slots(stop)
            await self._wait_for_work(stop)
        await self._shut_down()
        logger.info("worker stopped", extra={"worker_id": self.worker_id})

    async def run_until_idle(self) -> None:
        """Process every due job, then return (for tests and one-off runs)."""
        while True:
            await self._fill_free_slots(asyncio.Event())
            if not self._in_flight:
                return
            await asyncio.wait(self._in_flight, return_when=asyncio.FIRST_COMPLETED)

    async def _fill_free_slots(self, stop: asyncio.Event) -> None:
        await self._fail_poison_jobs()
        while len(self._in_flight) < self._concurrency and not stop.is_set():
            job = await self._claim()
            if job is None:
                return
            task = asyncio.create_task(self._run(job), name=f"job-{job.id}")
            self._in_flight.add(task)
            task.add_done_callback(self._in_flight.discard)

    async def _fail_poison_jobs(self) -> None:
        """A job whose lease ran out with no attempts left probably kills its worker (OOM, a
        crash in a native library): fail it instead of handing it to the next worker."""
        error = retries_exhausted(self._max_attempts, LEASE_EXPIRED)
        try:
            async with self._unit_of_work() as uow:
                failed = await uow.jobs.fail_expired_at_max(
                    max_attempts=self._max_attempts, error=error
                )
                await uow.commit()
        except Exception:
            logger.exception("failing expired jobs failed")
            return
        if failed:
            logger.warning("jobs failed after their last worker stopped", extra={"count": failed})

    async def _claim(self) -> IngestJob | None:
        try:
            async with self._unit_of_work() as uow:
                job = await uow.jobs.claim(
                    worker_id=self.worker_id,
                    lease_seconds=self._lease_seconds,
                    max_attempts=self._max_attempts,
                )
                await uow.commit()
        except Exception:  # the database is down or restarting: keep polling, don't die
            logger.exception("claiming a job failed")
            return None
        return job

    async def _run(self, job: IngestJob) -> None:
        # Each task runs in its own context: these don't leak between jobs.
        job_id_var.set(str(job.id))
        request_id_var.set(job.request_id)
        logger.info("job claimed", extra={"attempt": job.attempts})
        try:
            await self._process(job)
        except asyncio.CancelledError:
            await asyncio.shield(self._release(job))
            raise
        except Exception:
            logger.exception("job processing crashed; the lease will hand it to another try")

    async def _release(self, job: IngestJob) -> None:
        """Hand an unfinished job back at shutdown, at once and with its attempt given back. A
        no-op if it was finished after all (the fence): finishing is never interrupted."""
        try:
            async with self._unit_of_work() as uow:
                released = await uow.jobs.release(job.id, worker_id=self.worker_id)
                await uow.commit()
        except Exception:
            logger.exception("releasing the job failed; it will be retried after its lease")
            return
        if released:
            logger.info("job released at shutdown")

    async def _wait_for_work(self, stop: asyncio.Event) -> None:
        """Until a slot frees up (when full) or the poll interval passes, or we're stopped."""
        stopped = asyncio.create_task(stop.wait())
        # Any: tasks with different results; only their completion matters here.
        waiting_for: set[asyncio.Task[Any]] = {stopped}
        if len(self._in_flight) >= self._concurrency:
            waiting_for |= self._in_flight
        try:
            await asyncio.wait(
                waiting_for, timeout=self._poll_interval, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            stopped.cancel()

    async def _shut_down(self) -> None:
        """Let in-flight jobs finish for up to `shutdown_timeout`, then cancel (and so release)
        the rest. Stopped claiming already: `run` checks `stop` before every claim."""
        if not self._in_flight:
            return
        logger.info("finishing in-flight jobs", extra={"count": len(self._in_flight)})
        _, unfinished = await asyncio.wait(self._in_flight, timeout=self._shutdown_timeout)
        for task in unfinished:
            task.cancel()
        await asyncio.gather(*unfinished, return_exceptions=True)
