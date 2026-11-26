from uuid import UUID

from fastapi import APIRouter

from src.api.deps import GetIngestionDep
from src.api.schemas.error import STORAGE_UNAVAILABLE, ErrorResponse
from src.api.schemas.ingestion import IngestionResponse

router = APIRouter(prefix="/ingestions", tags=["ingestions"])


@router.get(
    "/{job_id}",
    responses={
        404: {"model": ErrorResponse, "description": "Ingestion not found"},
        **STORAGE_UNAVAILABLE,
    },
)
async def get_ingestion(job_id: UUID, service: GetIngestionDep) -> IngestionResponse:
    return IngestionResponse.from_job(await service.execute(job_id))
