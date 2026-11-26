import logging
from collections.abc import Awaitable, Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.adapters.clock import SystemClock
from src.adapters.documents import PdfiumDocumentReader
from src.adapters.extraction_failure_cache import RedisExtractionFailureCache
from src.adapters.parsers.llm import LlmInvoiceParser, extraction_config
from src.adapters.parsers.qr_bill import QrBillParser
from src.adapters.persistence.database import (
    create_engine,
    create_session_factory,
    database_readiness,
)
from src.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.adapters.rate_limiter import RedisRateLimiter
from src.adapters.redis_client import create_redis_client
from src.application.llm_quota import LlmQuota
from src.application.ports.clock import Clock
from src.application.ports.document_reader import DocumentReader
from src.application.ports.extraction_failure_cache import ExtractionFailureCache
from src.application.ports.invoice_parser import InvoiceParser
from src.application.ports.rate_limiter import RateLimiter
from src.application.ports.readiness import ReadinessCheck
from src.application.ports.unit_of_work import UnitOfWork, UnitOfWorkFactory
from src.application.retry_policy import RetryPolicy
from src.config import Settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class AppContext:
    """The long-lived objects of one process (API or worker), as ports.

    Short-lived objects (sessions, units of work, services) are built per request or per job from
    these. The raw clients (engine, Redis, Anthropic) aren't exposed: only `exit_stack` owns them.
    """

    settings: Settings
    unit_of_work: UnitOfWorkFactory
    database_ready: ReadinessCheck
    clock: Clock
    rate_limiter: RateLimiter
    llm_quota: LlmQuota
    llm_failure_cache: ExtractionFailureCache
    document_reader: DocumentReader
    qr_parser: InvoiceParser
    llm_parser: InvoiceParser | None  # None: no API key, the LLM fallback is off
    retry_policy: RetryPolicy
    exit_stack: AsyncExitStack = field(default_factory=AsyncExitStack, repr=False)

    async def aclose(self) -> None:
        """Closes the resources in reverse order of creation. Never raises for a failing cleanup
        (see `_close_quietly`), so it can't mask the reason the process is shutting down."""
        await self.exit_stack.aclose()
        logger.info("app context closed")


async def build_context(settings: Settings) -> AppContext:
    """Nothing here connects: the engine, Redis and Anthropic clients connect on first use.

    Each resource's cleanup is registered right after it's created. If anything fails halfway,
    leaving the `async with` closes what was already created and the original error propagates.
    """
    async with AsyncExitStack() as stack:
        engine = create_engine(settings.database_url)
        stack.push_async_callback(_close_quietly, "database engine", engine.dispose)
        session_factory = create_session_factory(engine)

        redis = _redis_from(settings)
        stack.push_async_callback(_close_quietly, "redis", redis.aclose)

        anthropic = _anthropic_from(settings)
        if anthropic is not None:
            stack.push_async_callback(_close_quietly, "anthropic", anthropic.close)

        config = extraction_config(settings.anthropic_model)
        logger.debug(
            "LLM extraction fingerprint %s (model %s)", config.fingerprint, settings.anthropic_model
        )
        rate_limiter = RedisRateLimiter(redis)
        llm_parser = None if anthropic is None else LlmInvoiceParser(anthropic, config)
        llm_quota = LlmQuota(
            rate_limiter,
            per_client_per_hour=settings.llm_requests_per_client_per_hour,
            per_day=settings.llm_requests_per_day,
        )
        llm_failure_cache = RedisExtractionFailureCache(
            redis,
            git_sha=settings.git_sha,
            fingerprint=config.fingerprint,
            ttl_seconds=settings.llm_failure_cache_ttl_seconds,
        )
        retry_policy = RetryPolicy(
            max_attempts=settings.job_max_attempts,
            base_seconds=settings.job_retry_base_seconds,
            cap_seconds=settings.job_retry_cap_seconds,
        )
        context = AppContext(
            settings=settings,
            unit_of_work=_unit_of_work_factory(session_factory),
            database_ready=database_readiness(engine),
            clock=SystemClock(),
            rate_limiter=rate_limiter,
            llm_quota=llm_quota,
            llm_failure_cache=llm_failure_cache,
            document_reader=PdfiumDocumentReader(
                max_pages=settings.max_pdf_pages, max_page_pixels=settings.max_pdf_page_pixels
            ),
            qr_parser=QrBillParser(),
            llm_parser=llm_parser,
            retry_policy=retry_policy,
            # Evaluated last: only now does the context own the cleanups, so leaving the block
            # closes nothing. Any earlier failure still closes everything created so far.
            exit_stack=stack.pop_all(),
        )

    # Only booleans and the fingerprint: never keys, DSNs or hosts.
    logger.info(
        "app context ready",
        extra={
            "llm_fallback_configured": anthropic is not None,
            "extraction_fingerprint": config.fingerprint,
        },
    )
    return context


async def _close_quietly(resource: str, close: Callable[[], Awaitable[object]]) -> None:
    """One failing cleanup must neither skip the others nor replace the error that caused the
    shutdown (the exit stack would re-raise it at the end). Logs only the error type."""
    try:
        await close()
    except Exception as error:
        logger.warning(
            "closing a resource failed",
            extra={"resource": resource, "error_type": type(error).__name__},
        )


def _unit_of_work_factory(session_factory: async_sessionmaker[AsyncSession]) -> UnitOfWorkFactory:
    def unit_of_work() -> UnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    return unit_of_work


def _redis_from(settings: Settings) -> Redis:
    return create_redis_client(
        host=settings.redis_host,
        port=settings.redis_port,
        db=settings.redis_db,
        username=settings.redis_user or None,
        password=settings.redis_password.get_secret_value() if settings.redis_password else None,
    )


def _anthropic_from(settings: Settings) -> AsyncAnthropic | None:
    """None without an API key: QR invoices still work, jobs without a QR code fail clearly."""
    api_key = settings.anthropic_api_key
    if api_key is None or not api_key.get_secret_value():
        return None
    return AsyncAnthropic(
        api_key=api_key.get_secret_value(),
        timeout=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
    )
