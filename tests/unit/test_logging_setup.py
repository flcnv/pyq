import logging
from datetime import UTC, datetime, timedelta

from src.logging_setup import APP_LOGGER, HANDLER_NAME, configure_logging, request_id_var
from tests.unit.logs import app_logs

logger = logging.getLogger("src.tests")


class NotJson:
    def __str__(self) -> str:
        return "<not json>"


def test_json_is_one_valid_object_per_line() -> None:
    with app_logs() as logs:
        logger.info("first")
        logger.warning("second\nwith a newline")

    first, second = logs.json()
    assert len(logs.lines) == 2  # the newline stays inside the JSON string
    assert first["message"] == "first"
    assert second["message"] == "second\nwith a newline"
    assert (second["level"], second["logger"], second["version"]) == (
        "WARNING",
        "src.tests",
        "abc1234",
    )
    assert datetime.fromisoformat(first["timestamp"]).utcoffset() == timedelta(0)  # UTC


def test_json_includes_every_extra_field() -> None:
    with app_logs() as logs:
        logger.info("invoice ingested", extra={"invoice_id": "42", "warning_count": 2})

    [entry] = logs.json()
    assert entry["invoice_id"] == "42"
    assert entry["warning_count"] == 2
    assert not {"args", "msg", "levelno", "pathname"} & entry.keys()  # standard attributes


def test_json_serializes_the_exception() -> None:
    with app_logs() as logs:
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("unhandled error")

    [entry] = logs.json()
    assert "Traceback" in entry["exception"]
    assert "ValueError: boom" in entry["exception"]


def test_a_non_serializable_extra_does_not_break_logging() -> None:
    with app_logs() as logs:
        logger.info(
            "odd extra", extra={"thing": NotJson(), "when": datetime(2026, 11, 23, tzinfo=UTC)}
        )

    [entry] = logs.json()
    assert entry["thing"] == "<not json>"
    assert entry["when"] == "2026-11-23 00:00:00+00:00"


def test_request_id_is_added_when_set() -> None:
    with app_logs() as logs:
        logger.info("outside a request")
        token = request_id_var.set("req-123")
        try:
            logger.info("inside a request")
        finally:
            request_id_var.reset(token)

    outside, inside = logs.json()
    assert "request_id" not in outside
    assert inside["request_id"] == "req-123"


def test_text_format_is_unchanged() -> None:
    with app_logs(log_format="text") as logs:
        logger.info("invoice ingested", extra={"invoice_id": "42"})

    assert logs.lines == ["INFO      src.tests: invoice ingested"]


def test_level_filters_records() -> None:
    with app_logs(level="INFO") as logs:
        logger.debug("hidden")
        logger.info("shown")

    assert [entry["message"] for entry in logs.json()] == ["shown"]


def test_configure_logging_is_idempotent_and_leaves_libraries_alone() -> None:
    app_logger = logging.getLogger(APP_LOGGER)
    library_level = logging.getLogger("sqlalchemy").level
    with app_logs():
        configure_logging(level="INFO", log_format="text", version="v2")

        assert [h.name for h in app_logger.handlers].count(HANDLER_NAME) == 1
        [handler] = [h for h in app_logger.handlers if h.name == HANDLER_NAME]
        assert len(handler.filters) == 1
        assert logging.getLogger("sqlalchemy").level == library_level


def test_text_format_appends_the_request_id_before_any_traceback() -> None:
    with app_logs(log_format="text") as logs:
        token = request_id_var.set("req-123")
        try:
            raise ValueError("boom")
        except ValueError:
            logger.exception("unhandled error")
        finally:
            request_id_var.reset(token)

    assert logs.lines[0] == "ERROR     src.tests: unhandled error request_id=req-123"
    assert logs.lines[1].startswith("Traceback")
