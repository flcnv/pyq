import logging
import os
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.api.errors import register_exception_handlers
from src.api.request_id import RequestIdMiddleware
from src.api.routes import health, ingestions, invoices
from src.api.upload_limit import UploadLimitMiddleware
from src.bootstrap import build_context
from src.config import get_settings
from src.logging_setup import configure_logging

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    settings = get_settings()
    configure_logging(
        level=settings.log_level, log_format=settings.log_format, version=settings.git_sha
    )
    warn_if_every_proxy_is_trusted(os.environ)
    context = await build_context(settings)
    app.state.context = context
    try:
        yield
    finally:
        await context.aclose()


def warn_if_every_proxy_is_trusted(environ: Mapping[str, str]) -> None:
    """uvicorn takes the client IP from X-Forwarded-For only from these addresses.

    Reads the process environment, like uvicorn itself (not `.env`). With `*`, anyone who can
    reach the app directly can send any X-Forwarded-For and get a fresh rate limit per request.
    """
    # uvicorn trusts everyone only for exactly "*"; inside a list, "*" is an inert literal.
    if environ.get("FORWARDED_ALLOW_IPS") == "*":
        logger.warning(
            "FORWARDED_ALLOW_IPS trusts every address: X-Forwarded-For can be spoofed to bypass "
            "rate limits. Set it to the load balancer's addresses or subnet only."
        )


def create_app() -> FastAPI:
    app = FastAPI(title="Swiss Invoice API", version="0.1.0", lifespan=lifespan)

    register_exception_handlers(app)

    app.add_middleware(UploadLimitMiddleware)
    app.add_middleware(RequestIdMiddleware)

    app.include_router(health.router)
    app.include_router(invoices.router)
    app.include_router(ingestions.router)

    return app


app = create_app()
