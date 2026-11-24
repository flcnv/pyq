import io
import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from src.logging_setup import APP_LOGGER, HANDLER_NAME, LogFormat, configure_logging


class CapturedLogs:
    def __init__(self, stream: io.StringIO) -> None:
        self._stream = stream

    @property
    def lines(self) -> list[str]:
        return self._stream.getvalue().splitlines()

    def json(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.lines]


@contextmanager
def app_logs(log_format: LogFormat = "json", level: str = "DEBUG") -> Iterator[CapturedLogs]:
    """The app's real logging setup, writing to a buffer; the previous setup is restored after."""
    app_logger = logging.getLogger(APP_LOGGER)
    saved_level, saved_handlers = app_logger.level, list(app_logger.handlers)
    app_logger.handlers = [h for h in app_logger.handlers if h.name != HANDLER_NAME]
    try:
        configure_logging(level=level, log_format=log_format, version="abc1234")
        [handler] = [h for h in app_logger.handlers if h.name == HANDLER_NAME]
        assert isinstance(handler, logging.StreamHandler)
        stream = io.StringIO()
        handler.setStream(stream)
        yield CapturedLogs(stream)
    finally:
        app_logger.handlers = saved_handlers
        app_logger.setLevel(saved_level)
