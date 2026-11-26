from typing import Any
from uuid import UUID

import pytest

from src.logging_setup import LogFormat
from tests.unit.api.test_invoices_api import BrokenJobRepository, api, pdf, upload
from tests.unit.application.fakes import InMemoryUnitOfWork
from tests.unit.logs import app_logs

IBAN = "CH6431961000004421557"
ACCESS_LOGGER = "src.api.access"


def access_lines(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [entry for entry in entries if entry["logger"] == ACCESS_LOGGER]


async def test_the_query_string_is_never_logged() -> None:
    with app_logs() as logs:
        async with api() as (client, _):
            response = await client.get(
                f"/invoices?creditor_iban={IBAN}&currency=CHF", headers={"X-Request-ID": "r1"}
            )

    assert response.status_code == 200
    assert IBAN not in "\n".join(logs.lines)
    assert "?" not in "\n".join(logs.lines)
    [line] = access_lines(logs.json())
    assert line["level"] == "INFO"
    assert line["message"].startswith("GET /invoices 200 ")
    assert (line["method"], line["path"], line["status"]) == ("GET", "/invoices", 200)
    assert isinstance(line["duration_ms"], float)
    assert line["duration_ms"] >= 0
    assert line["request_id"] == "r1"


@pytest.mark.parametrize("log_format", ["text", "json"])
async def test_text_and_json_formats_log_path_status_duration_and_request_id(
    log_format: LogFormat,
) -> None:
    with app_logs(log_format=log_format) as logs:
        async with api() as (client, _):
            await client.get(f"/invoices?creditor_iban={IBAN}", headers={"X-Request-ID": "r2"})

    [line] = [line for line in logs.lines if ACCESS_LOGGER in line]
    assert IBAN not in line
    assert "creditor_iban" not in line
    assert "GET /invoices 200 " in line
    assert "ms" in line
    assert "r2" in line


async def test_the_route_template_is_logged_not_the_id() -> None:
    invoice_id = UUID(int=7)
    with app_logs() as logs:
        async with api() as (client, _):
            response = await client.get(f"/invoices/{invoice_id}")

    assert response.status_code == 404
    [line] = access_lines(logs.json())
    assert (line["path"], line["status"]) == ("/invoices/{invoice_id}", 404)
    assert str(invoice_id) not in "\n".join(logs.lines)


async def test_an_unknown_path_is_logged_raw_without_its_query_string() -> None:
    with app_logs() as logs:
        async with api() as (client, _):
            response = await client.get(f"/nowhere?creditor_iban={IBAN}")

    assert response.status_code == 404
    [line] = access_lines(logs.json())
    assert (line["path"], line["status"]) == ("/nowhere", 404)
    assert IBAN not in "\n".join(logs.lines)


async def test_an_unknown_path_cannot_forge_a_text_log_line() -> None:
    with app_logs(log_format="text") as logs:
        async with api() as (client, _):
            await client.get("/a%0AINFO%20forged")

    [line] = logs.lines
    assert "/a%0AINFO%20forged 404 " in line


async def test_a_rate_limited_request_is_logged_as_429() -> None:
    with app_logs() as logs:
        async with api() as (client, _):
            responses = [await upload(client, pdf(number)) for number in range(4)]

    assert [response.status_code for response in responses][-1] == 429
    statuses = [
        (line["method"], line["path"], line["status"]) for line in access_lines(logs.json())
    ]
    assert statuses[-1] == ("POST", "/invoices", 429)
    assert len(statuses) == 4


async def test_a_request_whose_handler_raises_is_logged_as_500() -> None:
    with app_logs() as logs:
        async with api(uow=InMemoryUnitOfWork(jobs=BrokenJobRepository())) as (client, _):
            response = await upload(client, headers={"X-Request-ID": "r500"})

    assert response.status_code == 500
    [line] = access_lines(logs.json())
    assert (line["path"], line["status"], line["request_id"]) == ("/invoices", 500, "r500")


@pytest.mark.parametrize("path", ["/health", "/ready"])
async def test_probes_are_logged_only_at_debug(path: str) -> None:
    with app_logs(level="INFO") as info_logs:
        async with api() as (client, _):
            await client.get(path)
    with app_logs(level="DEBUG") as debug_logs:
        async with api() as (client, _):
            await client.get(path)

    assert info_logs.lines == []
    [line] = access_lines(debug_logs.json())
    assert (line["level"], line["path"], line["status"]) == ("DEBUG", path, 200)


async def test_headers_and_bodies_are_not_logged() -> None:
    with app_logs() as logs:
        async with api() as (client, _):
            await upload(
                client,
                ("secret-name.pdf", b"%PDF-1.7 secret body", "application/pdf"),
                headers={"Authorization": "Bearer secret-token"},
            )

    output = "\n".join(logs.lines)
    assert "secret" not in output
