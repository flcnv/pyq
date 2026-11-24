import math
from functools import lru_cache

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

from src.logging_setup import LogFormat


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    git_sha: str = "dev"  # the build's commit; baked into the image (Dockerfile ARG GIT_SHA)
    api_port: int = 8000
    log_level: str = "INFO"
    log_format: LogFormat = "text"

    postgres_user: str = "postgres"
    postgres_password: SecretStr = SecretStr("postgres")
    postgres_db: str = "postgres"
    postgres_host: str = "localhost"
    postgres_port: int = 5432

    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_user: str | None = None
    redis_password: SecretStr | None = None

    anthropic_api_key: SecretStr | None = None
    anthropic_model: str = "claude-sonnet-5-5"
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2

    max_upload_mb: int = Field(default=5, gt=0)
    max_pdf_pages: int = Field(default=10, ge=1)
    # Bounds the memory per rendered page. At 300 dpi, A4 is ~8.7 million pixels and A3 ~17.4
    # million; a larger page (or a huge MediaBox) is rendered at a lower dpi instead.
    max_pdf_page_pixels: int = Field(default=25_000_000, gt=0)

    uploads_per_client_per_minute: int = 20
    llm_requests_per_client_per_hour: int = 10
    llm_requests_per_day: int = 200
    llm_failure_cache_ttl_seconds: int = 24 * 60 * 60

    worker_concurrency: int = 2
    job_max_attempts: int = 5
    job_retry_base_seconds: float = 2.0
    job_retry_cap_seconds: float = 300.0
    worker_poll_interval_seconds: float = 1.0
    worker_shutdown_timeout_seconds: float = 30.0
    # Retry waits (the SDK honours Retry-After up to 60 s), rendering and the DB writes.
    job_lease_margin_seconds: int = 150

    @property
    def max_upload_bytes(self) -> int:
        return self.max_upload_mb * 1024 * 1024

    @property
    def job_lease_seconds(self) -> int:
        """Longer than the slowest possible job, so a lease can't run out while it's processed."""
        llm_worst_case = self.llm_timeout_seconds * (self.llm_max_retries + 1)
        return math.ceil(llm_worst_case) + self.job_lease_margin_seconds

    @property
    def database_url(self) -> URL:
        return URL.create(
            drivername="postgresql+asyncpg",
            username=self.postgres_user,
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.postgres_port,
            database=self.postgres_db,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
