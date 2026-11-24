import logging
from collections.abc import Awaitable, Callable
from http import HTTPStatus

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException
from starlette.responses import Response

from src.api.request_id import HEADER
from src.api.schemas.error import ErrorBody, ErrorDetail, ErrorResponse
from src.application.errors import (
    IngestionNotFoundError,
    InvoiceNotFoundError,
    RateLimitExceededError,
    UnreadableDocumentError,
)
from src.application.job_errors import job_error_for
from src.logging_setup import request_id_var

logger = logging.getLogger(__name__)


class ApiError(Exception):
    """An error raised by the API layer itself (upload checks, unwired adapters)."""

    def __init__(self, status: HTTPStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class UploadTooLargeError(HTTPException):
    """An HTTPException so FastAPI lets it out of the form parsing (it turns anything else raised
    while reading the body into a generic 400); its own handler gives it the API's error shape."""

    def __init__(self, max_upload_mb: int) -> None:
        super().__init__(HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        self.message = f"File must be at most {max_upload_mb} MB"


def error_response(
    status: HTTPStatus,
    code: str,
    message: str,
    details: list[ErrorDetail] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message, details=details or []))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"), headers=headers)


def _api_error(error: ApiError) -> JSONResponse:
    return error_response(error.status, error.code, error.message)


def _known_error_response(
    status: HTTPStatus, error: Exception, headers: dict[str, str] | None = None
) -> JSONResponse:
    """The code, message and details from the mapping that failed ingestion jobs use too."""
    job_error = job_error_for(error)
    if job_error is None:  # can't happen: only called for errors the mapping knows
        raise error
    body = ErrorResponse(error=ErrorBody.from_job_error(job_error))
    return JSONResponse(status_code=status, content=body.model_dump(mode="json"), headers=headers)


def _unreadable_document(error: UnreadableDocumentError) -> JSONResponse:
    return _known_error_response(HTTPStatus.BAD_REQUEST, error)


def _upload_too_large(error: UploadTooLargeError) -> JSONResponse:
    return error_response(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "file_too_large", error.message)


def _ingestion_not_found(error: IngestionNotFoundError) -> JSONResponse:
    return error_response(HTTPStatus.NOT_FOUND, "ingestion_not_found", str(error))


def _invoice_not_found(error: InvoiceNotFoundError) -> JSONResponse:
    return error_response(HTTPStatus.NOT_FOUND, "invoice_not_found", str(error))


def _rate_limited(error: RateLimitExceededError) -> JSONResponse:
    return error_response(
        HTTPStatus.TOO_MANY_REQUESTS,
        "rate_limited",
        str(error),
        headers={"Retry-After": str(error.retry_after_seconds)},
    )


def _request_validation(error: RequestValidationError) -> JSONResponse:
    details = [
        ErrorDetail(
            code=str(item["type"]),
            field=".".join(str(part) for part in item["loc"]),
            message=str(item["msg"]),
        )
        for item in error.errors()
    ]
    return error_response(
        HTTPStatus.UNPROCESSABLE_CONTENT, "request_validation", "Invalid request", details
    )


def _http_exception(error: HTTPException) -> JSONResponse:
    """Framework errors (unknown route, wrong method, ...) in the same error shape."""
    status = HTTPStatus(error.status_code)
    code = status.phrase.lower().replace(" ", "_")
    return error_response(status, code, str(error.detail))


def _unexpected(error: Exception) -> JSONResponse:
    # Full details go to the server log only; the client never sees internals or a traceback.
    logger.exception("unhandled error", exc_info=error)
    # This runs in Starlette's outermost middleware, past the request ID middleware's send, so the
    # header is added here; the ID lets the client's report be matched to this log line.
    request_id = request_id_var.get()
    return error_response(
        HTTPStatus.INTERNAL_SERVER_ERROR,
        "internal_error",
        "An unexpected error occurred",
        headers={HEADER: request_id} if request_id else None,
    )


type _AsyncHandler = Callable[[Request, Exception], Awaitable[Response]]


def _typed[E: Exception](kind: type[E], handle: Callable[[E], JSONResponse]) -> _AsyncHandler:
    """Adapt a handler for one exception type to Starlette's `(Request, Exception)` signature."""

    async def handler(_: Request, error: Exception) -> Response:
        if not isinstance(error, kind):  # can't happen: Starlette dispatches by type
            raise error
        return handle(error)

    return handler


def register_exception_handlers(app: FastAPI) -> None:
    handlers: list[tuple[type[Exception], _AsyncHandler]] = [
        (ApiError, _typed(ApiError, _api_error)),
        (UnreadableDocumentError, _typed(UnreadableDocumentError, _unreadable_document)),
        (InvoiceNotFoundError, _typed(InvoiceNotFoundError, _invoice_not_found)),
        (IngestionNotFoundError, _typed(IngestionNotFoundError, _ingestion_not_found)),
        (RateLimitExceededError, _typed(RateLimitExceededError, _rate_limited)),
        (RequestValidationError, _typed(RequestValidationError, _request_validation)),
        # Starlette picks the handler of the most specific class (by MRO), not the first registered.
        (UploadTooLargeError, _typed(UploadTooLargeError, _upload_too_large)),
        (HTTPException, _typed(HTTPException, _http_exception)),
        (Exception, _typed(Exception, _unexpected)),
    ]
    for kind, handler in handlers:
        app.add_exception_handler(kind, handler)
