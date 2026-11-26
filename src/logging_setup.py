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

# Record attribute → the context variable that fills it. Each is logged only when set.
_CONTEXT_FIELDS: dict[str, ContextVar[str | None]] = {
    "request_id": request_id_var,
    "job_id": job_id_var,
}

# Attributes every LogRecord has. Anything else on a record came from `extra={...}`.
_STANDARD_ATTRIBUTES = (
    frozenset(vars(logging.LogRecord("", logging.INFO, "", 0, "", None, None)))
    | {"message", "asctime"}
    | _CONTEXT_FIELDS.keys()
)


def _context_values(record: logging.LogRecord) -> dict[str, str]:
    """The record's context fields that are set, in `_CONTEXT_FIELDS` order."""
    values = {name: getattr(record, name, None) for name in _CONTEXT_FIELDS}
    return {name: value for name, value in values.items() if value is not None}


def _extra_fields(record: logging.LogRecord) -> dict[str, object]:
    return {key: value for key, value in vars(record).items() if key not in _STANDARD_ATTRIBUTES}


class ContextFilter(logging.Filter):
    """Stamps each record with the current context fields (None when unset)."""

    def filter(self, record: logging.LogRecord) -> bool:
        for name, var in _CONTEXT_FIELDS.items():
            setattr(record, name, var.get())
        return True


class TextFormatter(logging.Formatter):
    """`TEXT_FORMAT` plus the context fields that are set, so text lines can be traced too."""

    def __init__(self) -> None:
        super().__init__(TEXT_FORMAT)

    # formatMessage, not format: the IDs go on the record's first line, before any traceback.
    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802 (stdlib override)
        line = super().formatMessage(record)
        for name, value in _context_values(record).items():
            line += f" {name}={value}"
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
            **_context_values(record),
        }
        # An extra never overrides a field set above.
        for key, value in _extra_fields(record).items():
            entry.setdefault(key, value)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            entry["stack"] = self.formatStack(record.stack_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def _app_handler(app_logger: logging.Logger) -> logging.Handler:
    """The app's handler on `app_logger`, created on the first call."""
    handler = next((h for h in app_logger.handlers if h.name == HANDLER_NAME), None)
    if handler is None:
        handler = logging.StreamHandler()
        handler.name = HANDLER_NAME
        app_logger.addHandler(handler)
    # On the handler, not the logger: logger filters skip records from child loggers (src.api...).
    if not any(isinstance(f, ContextFilter) for f in handler.filters):
        handler.addFilter(ContextFilter())
    return handler


def configure_logging(*, level: str, log_format: LogFormat, version: str) -> None:
    """Give the app's own loggers (`src.*`) one handler, level and format.

    uvicorn configures only its own loggers, so without this the app's INFO and DEBUG logs go
    nowhere. Library loggers are left alone, so DEBUG here doesn't flood
    the output with SQLAlchemy or HTTP internals. Safe to call again: it reconfigures in place.
    """
    app_logger = logging.getLogger(APP_LOGGER)
    app_logger.setLevel(level.upper())
    formatter = JsonFormatter(version=version) if log_format == "json" else TextFormatter()
    _app_handler(app_logger).setFormatter(formatter)
