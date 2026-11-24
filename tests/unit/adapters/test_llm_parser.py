import copy
import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import httpx2
import pytest

from src.adapters.parsers.llm import (
    MAX_TEXT_CHARS,
    TRUNCATION_MARKER,
    ExtractionConfig,
    LlmInvoiceParser,
    truncate_text,
)
from src.application.errors import ExtractionFailedError, UnusableLlmOutputError
from src.application.ports.document_reader import Document
from src.domain.errors import InvalidInvoiceError
from src.domain.models.amount import Currency
from src.domain.models.invoice import Invoice, InvoiceSource
from src.domain.models.reference import ReferenceType
from src.domain.models.validation_issue import IssueCode
from src.domain.validation import ensure_valid
from tests.unit.adapters.claude_stub import EXAMPLE_1_EXTRACTION, ClaudeStub, message_body
from tests.unit.domain.factories import QR_IBAN, QR_REFERENCE

MODEL = "claude-test-model"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
DOCUMENT = Document(pages=(b"page.png",), text="Max Muster & Söhne\nBetrag: CHF 50.00")


def extraction(**changes: Any) -> dict[str, Any]:
    data = copy.deepcopy(EXAMPLE_1_EXTRACTION)
    data.update(changes)
    return data


async def parse(stub: ClaudeStub, document: Document = DOCUMENT) -> Invoice:
    parser = LlmInvoiceParser(stub.client, ExtractionConfig(model=MODEL))
    return await parser.parse(document, invoice_id=uuid4(), created_at=NOW)


# --- Mapping -------------------------------------------------------------------------------------


async def test_maps_the_extraction_to_a_domain_invoice() -> None:
    invoice = await parse(ClaudeStub.returning(EXAMPLE_1_EXTRACTION))

    assert invoice.source is InvoiceSource.LLM
    assert invoice.created_at == NOW
    assert invoice.creditor.iban == QR_IBAN  # printed groups removed
    assert invoice.creditor.name == "Max Muster & Söhne"
    assert invoice.creditor.address.building_number == "123"
    assert invoice.amount.value == Decimal("50.00")
    assert invoice.amount.currency is Currency.CHF
    assert invoice.debtor is not None
    assert invoice.debtor.name == "Simon Muster"
    assert invoice.reference.type is ReferenceType.QRR
    assert invoice.reference.value == QR_REFERENCE  # printed groups removed
    assert invoice.unstructured_message == "Payment of travel"
    assert invoice.alternative_procedures == ()


async def test_a_correct_extraction_passes_domain_validation() -> None:
    invoice = ensure_valid(await parse(ClaudeStub.returning(EXAMPLE_1_EXTRACTION)))

    assert invoice.warnings == ()


async def test_llm_output_is_not_trusted_blindly() -> None:
    # One wrong digit: schema-valid JSON, but the IBAN check digits catch it.
    wrong_iban = extraction(creditor_iban="CH64 3196 1000 0044 2155 8")
    invoice = await parse(ClaudeStub.returning(wrong_iban))

    with pytest.raises(InvalidInvoiceError) as error:
        ensure_valid(invoice)
    assert IssueCode.INVALID_IBAN in {issue.code for issue in error.value.issues}


@pytest.mark.parametrize(
    ("printed", "expected"),
    [
        ("50.00", Decimal("50.00")),
        ("1 949.75", Decimal("1949.75")),  # ch. 3.5.3: blank as thousands separator
        ("1'949.75", Decimal("1949.75")),  # common Swiss notation outside the payment part
        (None, None),  # amount left blank for the payer
        ("  ", None),
    ],
)
async def test_amount_becomes_a_decimal(printed: str | None, expected: Decimal | None) -> None:
    invoice = await parse(ClaudeStub.returning(extraction(amount=printed)))

    assert invoice.amount.value == expected


@pytest.mark.parametrize("printed", ["fifty francs", "NaN", "Infinity"])
async def test_an_amount_that_is_not_a_number_fails_extraction(printed: str) -> None:
    with pytest.raises(UnusableLlmOutputError):
        await parse(ClaudeStub.returning(extraction(amount=printed)))


async def test_blank_optional_values_become_none() -> None:
    blank = extraction(
        debtor=None,
        reference_type="NON",
        reference="",
        unstructured_message="  ",
        billing_information="",
    )
    blank["creditor"]["address"]["street"] = ""

    invoice = await parse(ClaudeStub.returning(blank))

    assert invoice.debtor is None
    assert invoice.reference.value is None
    assert invoice.unstructured_message is None
    assert invoice.billing_information is None
    assert invoice.creditor.address.street is None


# --- Request -------------------------------------------------------------------------------------


async def test_request_uses_structured_output_and_the_configured_model() -> None:
    stub = ClaudeStub.returning(EXAMPLE_1_EXTRACTION)

    await parse(stub)

    request = stub.last_request
    assert request["model"] == MODEL
    output_format = request["output_config"]["format"]
    assert output_format["type"] == "json_schema"
    assert output_format["schema"]["additionalProperties"] is False
    assert "tools" not in request


async def test_request_sends_only_the_document_text_as_untrusted_data() -> None:
    stub = ClaudeStub.returning(EXAMPLE_1_EXTRACTION)

    await parse(stub)

    [message] = stub.last_request["messages"]
    assert message["role"] == "user"
    assert message["content"] == f"<document>\n{DOCUMENT.text}\n</document>"
    assert "untrusted data" in stub.last_request["system"]


async def test_long_documents_are_truncated_before_sending() -> None:
    stub = ClaudeStub.returning(EXAMPLE_1_EXTRACTION)
    long_text = "x" * (MAX_TEXT_CHARS * 3)

    await parse(stub, Document(pages=(), text=long_text))

    content = stub.last_request["messages"][0]["content"]
    assert len(content) <= MAX_TEXT_CHARS + len("<document>\n\n</document>")


def test_short_text_is_not_truncated() -> None:
    text = "a" * MAX_TEXT_CHARS

    assert truncate_text(text) == text


def test_truncation_keeps_the_start_and_the_end() -> None:
    # The payment part sits at the bottom of the last page, so the end must survive.
    text = "HEAD" + "x" * MAX_TEXT_CHARS + "PAYMENT PART"

    truncated = truncate_text(text)

    assert len(truncated) == MAX_TEXT_CHARS
    assert truncated.startswith("HEAD")
    assert truncated.endswith("PAYMENT PART")
    assert TRUNCATION_MARKER in truncated


# --- Failures ------------------------------------------------------------------------------------


def api_error(status: int) -> httpx2.Response:
    body = {"type": "error", "error": {"type": "api_error", "message": "boom"}}
    return httpx2.Response(status, json=body, headers={"request-id": "req_test"})


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(api_error(500), id="server error"),
        pytest.param(api_error(529), id="overloaded"),
        pytest.param(api_error(429), id="rate limited"),
        pytest.param(api_error(401), id="bad api key"),
        pytest.param(api_error(400), id="bad request"),
        pytest.param(httpx2.ConnectError("refused"), id="connection error"),
        pytest.param(httpx2.ReadTimeout("slow"), id="timeout"),
    ],
)
async def test_api_failures_become_extraction_failed(
    response: httpx2.Response | Exception,
) -> None:
    with pytest.raises(ExtractionFailedError) as raised:
        await parse(ClaudeStub(response))

    # Transient: must stay retryable, so never reported as unusable (deterministic) output.
    assert not isinstance(raised.value, UnusableLlmOutputError)


@pytest.mark.parametrize(
    ("text", "stop_reason"),
    [
        pytest.param("not json", "end_turn", id="not json"),
        pytest.param('{"creditor_iban": "CH64', "max_tokens", id="cut off at max_tokens"),
        pytest.param(json.dumps({"creditor_iban": "CH64"}), "end_turn", id="missing fields"),
        pytest.param(
            json.dumps(extraction(currency="USD")), "end_turn", id="value outside the schema"
        ),
        pytest.param("I can't help with that.", "refusal", id="refusal"),
    ],
)
async def test_unusable_output_becomes_extraction_failed(text: str, stop_reason: str) -> None:
    stub = ClaudeStub(httpx2.Response(200, json=message_body(text, stop_reason=stop_reason)))

    with pytest.raises(UnusableLlmOutputError):
        await parse(stub)


async def test_valid_json_with_a_bad_stop_reason_is_not_used() -> None:
    stub = ClaudeStub.returning(EXAMPLE_1_EXTRACTION, stop_reason="max_tokens")

    with pytest.raises(UnusableLlmOutputError):
        await parse(stub)


async def test_failures_never_log_document_contents(caplog: pytest.LogCaptureFixture) -> None:
    document_text = "IBAN CH64 3196 1000 0044 2155 7 Simon Muster"
    caplog.set_level(logging.INFO)

    with pytest.raises(ExtractionFailedError):
        await parse(ClaudeStub(api_error(500)), Document(pages=(), text=document_text))

    assert caplog.records  # the failure is logged...
    assert "CH64" not in caplog.text  # ...without the document
    assert "Simon Muster" not in caplog.text
