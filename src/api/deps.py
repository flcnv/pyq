from typing import Annotated

from fastapi import Depends, Request

from src.application.errors import RateLimiterUnavailableError
from src.application.ports.rate_limiter import RateLimit
from src.application.services.enqueue_ingestion import EnqueueIngestion
from src.application.services.get_ingestion import GetIngestion
from src.application.services.get_invoice import GetInvoice
from src.application.services.list_invoices import ListInvoices
from src.bootstrap import AppContext
from src.config import Settings
from src.logging_setup import request_id_var

# Thin FastAPI dependencies: they read the long-lived ports from the `AppContext` the lifespan
# built (`src/bootstrap.py`) and build the per-request services from them.


def get_context(request: Request) -> AppContext:
    context: AppContext | None = getattr(request.app.state, "context", None)
    if context is None:
        raise RuntimeError(
            "app.state.context is not set: the app's lifespan didn't run "
            "(in tests, set it to a context from tests/unit/context.py:make_context)"
        )
    return context


ContextDep = Annotated[AppContext, Depends(get_context)]


def get_app_settings(context: ContextDep) -> Settings:
    return context.settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_client_id(request: Request) -> str:
    """The client's IP. Behind a proxy, uvicorn's `--forwarded-allow-ips` sets the trusted hops."""
    return request.client.host if request.client else "unknown"


ClientIdDep = Annotated[str, Depends(get_client_id)]


def get_request_id() -> str | None:
    """The ID the request ID middleware gave this request."""
    return request_id_var.get()


RequestIdDep = Annotated[str | None, Depends(get_request_id)]


async def limit_uploads(context: ContextDep, client_id: ClientIdDep) -> None:
    """Fails open: if Redis is down, uploads still work (the LLM quota fails closed instead)."""
    rate_limit = RateLimit(
        name="uploads", limit=context.settings.uploads_per_client_per_minute, window_seconds=60
    )
    try:
        await context.rate_limiter.hit(rate_limit, client_id)
    except RateLimiterUnavailableError:
        return


def get_get_invoice(context: ContextDep) -> GetInvoice:
    return GetInvoice(unit_of_work=context.unit_of_work)


def get_list_invoices(context: ContextDep) -> ListInvoices:
    return ListInvoices(unit_of_work=context.unit_of_work)


def get_enqueue_ingestion(context: ContextDep) -> EnqueueIngestion:
    return EnqueueIngestion(unit_of_work=context.unit_of_work, clock=context.clock)


def get_get_ingestion(context: ContextDep) -> GetIngestion:
    return GetIngestion(unit_of_work=context.unit_of_work)


EnqueueIngestionDep = Annotated[EnqueueIngestion, Depends(get_enqueue_ingestion)]
GetIngestionDep = Annotated[GetIngestion, Depends(get_get_ingestion)]
GetInvoiceDep = Annotated[GetInvoice, Depends(get_get_invoice)]
ListInvoicesDep = Annotated[ListInvoices, Depends(get_list_invoices)]
