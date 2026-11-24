from typing import Protocol
from uuid import UUID

from src.application.models.ingest_job import IngestJob, JobError


class IngestJobRepository(Protocol):
    async def add(self, job: IngestJob) -> IngestJob:
        """Store a new job; returns it with `run_after`, `created_at` and `updated_at` set by the
        database clock, which every worker shares. Raises `JobAlreadyExistsError` if a job for
        the same content hash exists."""
        ...

    async def get(self, job_id: UUID) -> IngestJob | None: ...

    async def get_by_content_sha256(self, content_sha256: str) -> IngestJob | None: ...

    async def reset_failed(
        self, job_id: UUID, *, client_id: str, request_id: str | None
    ) -> IngestJob | None:
        """Requeue a failed job (attempts 0, error cleared). None if it wasn't `failed` anymore."""
        ...

    # --- The queue: every method below runs on the database clock ---------------------------

    async def claim(
        self, *, worker_id: str, lease_seconds: int, max_attempts: int
    ) -> IngestJob | None:
        """Atomically take the next due job, or one whose lease expired with attempts left:
        `processing`, locked by `worker_id` until now + lease, attempts + 1. Concurrent callers
        never get the same job (`FOR UPDATE SKIP LOCKED`). None if nothing is due."""
        ...

    async def fail_expired_at_max(self, *, max_attempts: int, error: JobError) -> int:
        """Fail jobs whose lease expired with no attempts left (poison jobs that kill their
        worker), instead of handing them out again. Returns how many."""
        ...

    # Fenced: each applies only if the job is still `processing` and locked by `worker_id`, and
    # returns False otherwise, i.e. when another worker took the job over after our lease ran out.

    async def complete(self, job_id: UUID, *, worker_id: str, invoice_id: UUID) -> bool: ...

    async def fail(self, job_id: UUID, *, worker_id: str, error: JobError) -> bool: ...

    async def retry_later(self, job_id: UUID, *, worker_id: str, delay_seconds: float) -> bool:
        """Back to `pending`, due again at now + delay. Attempts are kept."""
        ...

    async def release(self, job_id: UUID, *, worker_id: str) -> bool:
        """Back to `pending`, due now, with its attempt given back: the worker is shutting down,
        which isn't the job's fault."""
        ...
