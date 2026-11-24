from typing import Any
from uuid import UUID

import pytest
from starlette.types import Message, Receive, Scope, Send

from src.api.request_id import RequestIdMiddleware
from src.logging_setup import request_id_var
from tests.unit.api.test_invoices_api import BrokenJobRepository, api, upload
from tests.unit.application.fakes import InMemoryUnitOfWork
from tests.unit.logs import app_logs


def is_uuid4(value: str) -> bool:
    try:
        return UUID(value).version == 4
    except ValueError:
        return False


async def test_request_without_an_id_gets_a_new_uuid4() -> None:
    async with api() as (client, _):
        response = await client.get("/invoices")

    assert is_uuid4(response.headers["x-request-id"])


async def test_each_request_gets_its_own_id() -> None:
    async with api() as (client, _):
        first = await client.get("/invoices")
        second = await client.get("/invoices")

    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_a_safe_incoming_id_is_echoed_back() -> None:
    async with api() as (client, _):
        response = await client.get("/invoices", headers={"X-Request-ID": "trace-1.A_b"})

    assert response.headers["x-request-id"] == "trace-1.A_b"


async def test_error_responses_carry_the_id_too() -> None:
    async with api() as (client, _):
        not_found = await client.get(f"/invoices/{UUID(int=0)}", headers={"X-Request-ID": "r404"})

    assert not_found.status_code == 404
    assert not_found.headers["x-request-id"] == "r404"


async def test_the_id_is_in_every_log_line_of_the_request() -> None:
    with app_logs() as logs:
        async with api() as (client, _):
            await upload(client, ("a.pdf", b"%PDF-1.7 a", "application/pdf"))

    entries = logs.json()
    assert "ingestion queued" in [entry["message"] for entry in entries]
    assert {entry.get("request_id") for entry in entries} == {entries[0]["request_id"]}


async def test_unhandled_error_is_logged_and_answered_with_the_same_id() -> None:
    with app_logs() as logs:
        async with api(uow=InMemoryUnitOfWork(jobs=BrokenJobRepository())) as (client, _):
            response = await client.post(
                "/invoices",
                files={"file": ("a.pdf", b"%PDF-1.7 a", "application/pdf")},
                headers={"X-Request-ID": "r500"},
            )

    assert response.status_code == 500
    assert response.headers["x-request-id"] == "r500"
    [error_log] = [entry for entry in logs.json() if entry["message"] == "unhandled error"]
    assert error_log["request_id"] == "r500"
    assert "RuntimeError" in error_log["exception"]


# --- Unsafe incoming IDs: driven with a raw ASGI scope, since HTTP clients refuse to send them ---


async def call_with_header(value: bytes) -> tuple[str | None, list[tuple[bytes, bytes]]]:
    """Run the middleware around an app that records the request ID it sees."""
    seen: dict[str, Any] = {}

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen["request_id"] = request_id_var.get()
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    sent: list[Message] = []

    async def send(message: Message) -> None:
        sent.append(message)

    async def receive() -> Message:
        return {"type": "http.request", "body": b""}

    scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"x-request-id", value)]}
    token = request_id_var.set(None)
    try:
        await RequestIdMiddleware(app)(scope, receive, send)
    finally:
        request_id_var.reset(token)
    return seen["request_id"], sent[0]["headers"]


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(b"abc\nINFO forged log line", id="newline"),
        pytest.param(b"abc\r\nX-Injected: 1", id="CRLF"),
        pytest.param(b'quote"', id="quote"),
        pytest.param(b"has space", id="space"),
        pytest.param(b"a" * 65, id="too long"),
        pytest.param(b"", id="empty"),
        pytest.param("café".encode("latin-1"), id="non-ASCII"),
    ],
)
async def test_an_unsafe_incoming_id_is_replaced(value: bytes) -> None:
    request_id, headers = await call_with_header(value)

    assert request_id is not None
    assert is_uuid4(request_id)
    assert headers == [(b"x-request-id", request_id.encode())]


async def test_a_64_character_id_is_accepted() -> None:
    request_id, _ = await call_with_header(b"a" * 64)

    assert request_id == "a" * 64
