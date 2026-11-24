import logging
import re
import time
from urllib.parse import quote
from uuid import uuid4

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.logging_setup import request_id_var

HEADER = "X-Request-ID"
# Accept a caller's ID only if it can't break a log line (no newlines, quotes, control chars).
SAFE_REQUEST_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")
# Polled every few seconds by the Docker healthcheck: at DEBUG, so it doesn't drown the INFO log.
DEBUG_ONLY_PATHS = frozenset({"/health"})

# Replaces uvicorn's access log (run with --no-access-log), which writes the full URL: query
# strings like `?creditor_iban=CH...` must never reach the logs.
access_logger = logging.getLogger("src.api.access")


class RequestIdMiddleware:
    """Gives every request an ID and writes one access log line per request.

    The ID is the caller's `X-Request-ID` if safe, else a new UUID4. It goes into every app log
    record (via `request_id_var`) and back in the response header.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        started = time.perf_counter()
        request_id = _incoming_request_id(scope) or str(uuid4())
        # Not reset afterwards on purpose: the 500 handler runs outside this middleware (in
        # Starlette's ServerErrorMiddleware) and must still log and return this ID. Each request
        # runs in its own asyncio task, i.e. its own context, so the value can't leak.
        request_id_var.set(request_id)
        # A raised error has no status yet: ServerErrorMiddleware, outside this one, turns it into
        # the 500 after this line is written.
        status = 500

        async def send_with_request_id(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                headers.append((HEADER.lower().encode(), request_id.encode()))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self._app(scope, receive, send_with_request_id)
        finally:
            _log_request(scope, status, time.perf_counter() - started)


def _log_request(scope: Scope, status: int, seconds: float) -> None:
    """Method, path (never the query string), status and duration. No headers, no bodies."""
    path = _path_without_query(scope)
    duration_ms = round(seconds * 1000, 1)
    level = logging.DEBUG if path in DEBUG_ONLY_PATHS else logging.INFO
    access_logger.log(
        level,
        "%s %s %d %.1fms",
        scope["method"],
        path,
        status,
        duration_ms,
        extra={
            "method": scope["method"],
            "path": path,
            "status": status,
            "duration_ms": duration_ms,
        },
    )


def _path_without_query(scope: Scope) -> str:
    """The matched route's template (`/invoices/{id}`), else the path as the client sent it.

    The template keeps ids out of the line and groups requests per endpoint. An unmatched path
    is logged percent-encoded (`raw_path`), so a decoded `%0A` can't start a forged log line.
    """
    template = getattr(scope.get("route"), "path", None)
    if isinstance(template, str):
        return template
    raw_path: bytes | None = scope.get("raw_path")
    if raw_path is None:  # optional in ASGI: re-encode the decoded path instead
        return quote(scope["path"])
    # ASGI excludes the query string from raw_path, but don't rely on every server getting it right.
    return raw_path.partition(b"?")[0].decode("latin-1")


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope["headers"]:
        if name == HEADER.lower().encode():
            candidate: str = value.decode("latin-1")
            return candidate if SAFE_REQUEST_ID.fullmatch(candidate) else None
    return None
