from uuid import UUID

from src.application.errors import IngestionNotFoundError
from src.application.models.ingest_job import IngestJob
from src.application.ports.unit_of_work import UnitOfWorkFactory


class GetIngestion:
    def __init__(self, *, unit_of_work: UnitOfWorkFactory) -> None:
        self._unit_of_work = unit_of_work

    async def execute(self, job_id: UUID) -> IngestJob:
        async with self._unit_of_work() as uow:
            job = await uow.jobs.get(job_id)
        if job is None:
            raise IngestionNotFoundError(job_id)
        return job
