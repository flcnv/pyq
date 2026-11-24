from fastapi import APIRouter

from src.api.deps import SettingsDep
from src.api.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(settings: SettingsDep) -> HealthResponse:
    return HealthResponse(version=settings.git_sha)
