import json
import logging
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Literal

APP_LOGGER = "src"
HANDLER_NAME = "app"
TEXT_FORMAT = "%(levelname)-9s %(name)s: %(message)s"

type LogFormat = Literal["text", "json"]

# The current HTTP request's ID (set by the request ID middleware), and in the worker the ID of
# the upload that created the job, so one upload can be traced from the API to the worker.
request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)
# The ingestion job the worker is processing.
job_id_var: ContextVar[str | None] = ContextVar("job_id", default=None)

# Attributes every LogRecord has. Anything else on a record came from `extra={...}`.
_STANDARD_ATTRIBUTES = frozenset(
    vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None))
) | {"message", "asctime", "request_id", "job_id"}


class RequestIdFilter(logging.Filter):
    """Stamps each record with the current request ID and job ID (None when there is none)."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        record.job_id = job_id_var.get()
        return True


class TextFormatter(logging.Formatter):
    """`TEXT_FORMAT` plus the request ID and job ID when set, so text lines can be traced too."""

    def __init__(self) -> None:
        super().__init__(TEXT_FORMAT)

    # formatMessage, not format: the IDs go on the record's first line, before any traceback.
    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 (stdlib override)
        line = super().formatMessage(record)
        for context in ("request_id", "job_id"):
            value = getattr(record, context, None)
            if value is not None:
                line += f" {context}={value}"
        return line


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Never raises on odd extras: anything not JSON is `str()`-ed."""

    def __init__(self, *, version: str) -> None:
        super().__init__()
        self._version = version

    def format(self, record: logging.LogRecord) -> str:
        # Any: values are arbitrary JSON, and whatever callers pass in `extra`.
        entry: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "version": self._version,
        }
        for context in ("request_id", "job_id"):
            value = getattr(record, context, None)
            if value is not None:
                entry[context] = value
        for key, value in vars(record).items():
            if key not in _STANDARD_ATTRIBUTES and key not in entry:
                entry[key] = value
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(*, level: str, log_format: LogFormat, version: str) -> None:
    """Give the app's own loggers (`src.*`) one handler, level and format.

    uvicorn configures only its own loggers, so without this the app's INFO and DEBUG logs go
    nowhere. Library loggers are left alone, so DEBUG here doesn't flood
    the output with SQLAlchemy or HTTP internals. Safe to call again: it reconfigures in place.
    """
    app_logger = logging.getLogger(APP_LOGGER)
    app_logger.setLevel(level.upper())
    handler = next((h for h in app_logger.handlers if h.name == HANDLER_NAME), None)
    if handler is None:
        handler = logging.StreamHandler()
        handler.name = HANDLER_NAME
        app_logger.addHandler(handler)
    # On the handler, not the logger: logger filters skip records from child loggers (src.api...).
    if not any(isinstance(f, RequestIdFilter) for f in handler.filters):
        handler.addFilter(RequestIdFilter())
    formatter = JsonFormatter(version=version) if log_format == "json" else TextFormatter()
    handler.setFormatter(formatter)
