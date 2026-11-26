from fastapi import APIRouter

from src.api.deps import DatabaseReadyDep, SettingsDep
from src.api.schemas.error import STORAGE_UNAVAILABLE
from src.api.schemas.health import HealthResponse

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(settings: SettingsDep) -> HealthResponse:
    """Liveness: the process answers. Checks nothing else, so an outage elsewhere doesn't get
    the API restarted."""
    return HealthResponse(version=settings.git_sha)


@router.get("/ready", responses=STORAGE_UNAVAILABLE)
async def ready(settings: SettingsDep, database_ready: DatabaseReadyDep) -> HealthResponse:
    """Readiness: the database answers, so requests can be served. Redis isn't checked: without
    it the API degrades but still works."""
    await database_ready()
    return HealthResponse(version=settings.git_sha)
