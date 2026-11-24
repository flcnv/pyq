from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.api.deps import get_context
from src.api.errors import UploadTooLargeError

# Room for the multipart envelope around the file: boundaries, part headers, the file name.
# The file itself is checked exactly in the route.
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class UploadLimitMiddleware:
    """Stops reading a request body once it is larger than `MAX_UPLOAD_MB` plus the envelope.

    FastAPI parses (and spools) the whole multipart body before the route runs, so a limit checked
    in the route alone would come after an arbitrarily large body was received. This one acts while
    the body is read: an oversized `Content-Length` is refused before the first byte, a chunked
    body as soon as the bytes received so far pass the limit. The error is raised from `receive`,
    i.e. inside the form parsing, and becomes the usual `413 file_too_large` response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        received = 0

        async def receive_with_limit() -> Message:
            nonlocal received
            max_upload_mb, limit = _limits(scope)
            if _declared_length(scope) > limit:
                raise UploadTooLargeError(max_upload_mb)
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise UploadTooLargeError(max_upload_mb)
            return message

        await self._app(scope, receive_with_limit, send)


def _limits(scope: Scope) -> tuple[int, int]:
    """Read per request: the settings are in the context, which the lifespan builds."""
    settings = get_context(Request(scope)).settings
    return settings.max_upload_mb, settings.max_upload_bytes + MULTIPART_OVERHEAD_BYTES


def _declared_length(scope: Scope) -> int:
    try:
        return int(Headers(scope=scope).get("content-length", "0"))
    except ValueError:  # malformed: the server rejects it; the byte count still applies
        return 0
