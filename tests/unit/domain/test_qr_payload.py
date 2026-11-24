from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from src.domain.errors import InvalidInvoiceError, InvalidQrPayloadError
from src.domain.models.amount import Amount, Currency
from src.domain.models.invoice import Invoice, InvoiceSource
from src.domain.models.party import Address, Creditor, Debtor
from src.domain.models.reference import Reference, ReferenceType
from src.domain.models.validation_issue import IssueCode, IssueSeverity
from src.domain.qr_payload import parse_qr_payload
from src.domain.validation import ensure_valid

FIXTURES = Path(__file__).parents[2] / "fixtures"
INVOICE_ID = UUID("00000000-0000-4000-8000-000000000001")
CREATED_AT = datetime(2026, 11, 22, tzinfo=UTC)
VALID_EXAMPLES = [f"annex_a_example_{number}" for number in (1, 2, 3, 5, 6)]


def load_fixture(name: str) -> str:
    # Bytes, not read_text(): universal-newline mode would silently turn CRLF into LF.
    return (FIXTURES / f"{name}.txt").read_bytes().decode("utf-8")


def parse(payload: str) -> Invoice:
    return parse_qr_payload(payload, invoice_id=INVOICE_ID, created_at=CREATED_AT)


def example_1_lines() -> list[str]:
    return load_fixture("annex_a_example_1").split("\n")


def example_1_with(line_number: int, value: str) -> str:
    """Example 1 with one line replaced (1-based, as numbered in the spec)."""
    lines = example_1_lines()
    lines[line_number - 1] = value
    return "\n".join(lines)


def issue_codes(error: InvalidInvoiceError) -> list[tuple[IssueCode, str]]:
    return [(issue.code, issue.field) for issue in error.issues]


SELDWYLA = {"postal_code": "8000", "town": "Seldwyla", "country": "CH"}
MAX_MUSTER = Creditor(
    iban="CH6431961000004421557",
    name="Max Muster & Söhne",
    address=Address(street="Musterstrasse", building_number="123", **SELDWYLA),
)
SIMON_MUSTER = Debtor(
    name="Simon Muster",
    address=Address(street="Musterstrasse", building_number="1", **SELDWYLA),
)


# --- Spec examples -------------------------------------------------------------------------------


def test_parses_annex_a_example_1() -> None:
    assert parse(load_fixture("annex_a_example_1")) == Invoice(
        id=INVOICE_ID,
        created_at=CREATED_AT,
        source=InvoiceSource.QR,
        creditor=MAX_MUSTER,
        amount=Amount(value=Decimal("50.00"), currency=Currency.CHF),
        debtor=SIMON_MUSTER,
        reference=Reference(type=ReferenceType.QRR, value="000008207791225857421286694"),
        unstructured_message="Bezahlung der Reise",
        billing_information=None,
        alternative_procedures=(),
    )


def test_parses_annex_a_example_2_with_billing_information_and_alternative_procedure() -> None:
    invoice = parse(load_fixture("annex_a_example_2"))

    assert invoice.creditor.iban == "CH4431999123000889012"
    assert invoice.amount == Amount(value=Decimal("1949.75"), currency=Currency.CHF)
    assert invoice.unstructured_message == "Auftrag vom 15.10.2020"
    assert invoice.billing_information == "//S1/10/1234/11/201021/30/102673386/32/7.7/40/0:30"
    assert invoice.alternative_procedures == ("eBill/B/simon.muster@example.com",)


def test_parses_annex_a_example_3_without_amount_debtor_or_reference() -> None:
    invoice = parse(load_fixture("annex_a_example_3"))

    assert invoice.creditor == Creditor(
        iban="CH5204835012345671000",
        name="Muster Stiftung",
        address=Address(
            street="Postfach", building_number=None, postal_code="3001", town="Bern", country="CH"
        ),
    )
    assert invoice.amount == Amount(value=None, currency=Currency.CHF)
    assert invoice.debtor is None
    assert invoice.reference == Reference(type=ReferenceType.NON, value=None)
    assert invoice.unstructured_message is None


@pytest.mark.parametrize("name", VALID_EXAMPLES)
@pytest.mark.parametrize("separator", ["\n", "\r\n"], ids=["LF", "CRLF"])
def test_spec_examples_parse_and_pass_validation(name: str, separator: str) -> None:
    payload = load_fixture(name).replace("\n", separator)

    validated = ensure_valid(parse(payload))

    assert validated.warnings == ()


def test_annex_a_example_4_fails_validation_because_its_reference_is_wrong_in_the_spec() -> None:
    invoice = parse(load_fixture("annex_a_example_4"))

    with pytest.raises(InvalidInvoiceError) as raised:
        ensure_valid(invoice)

    assert issue_codes(raised.value) == [(IssueCode.INVALID_CREDITOR_REFERENCE, "reference.value")]


# --- Envelope: length, separators, line count ----------------------------------------------------


def payload_of_length(length: int) -> str:
    """Example 1 padded with alternative-procedure lines (status A) to an exact length."""
    payload = "\n".join(example_1_lines()[:32])  # up to and including empty billing info
    while len(payload) < length:
        remaining = length - len(payload) - 1  # 1 for the separator
        payload += "\n" + "A" * min(remaining, 99)
    assert len(payload) == length
    return payload


def test_payload_of_exactly_997_characters_is_accepted() -> None:
    assert parse(payload_of_length(997)).creditor.iban == "CH6431961000004421557"


def test_payload_of_998_characters_is_rejected() -> None:
    with pytest.raises(InvalidQrPayloadError) as raised:
        parse(payload_of_length(998))

    assert issue_codes(raised.value) == [(IssueCode.PAYLOAD_TOO_LONG, "payload")]


def test_payload_longer_than_997_characters_is_rejected() -> None:
    payload = load_fixture("annex_a_example_1") + "\n" + "\n".join(["A" * 99] * 8)

    with pytest.raises(InvalidQrPayloadError) as raised:
        parse(payload)

    assert issue_codes(raised.value) == [(IssueCode.PAYLOAD_TOO_LONG, "payload")]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(
            "\r\n".join(example_1_lines()[:5]) + "\n" + "\n".join(example_1_lines()[5:]),
            id="CRLF and LF mixed",
        ),
        pytest.param("\r".join(example_1_lines()), id="bare CR"),
    ],
)
def test_inconsistent_line_separators_are_rejected(payload: str) -> None:
    with pytest.raises(InvalidQrPayloadError) as raised:
        parse(payload)

    assert issue_codes(raised.value) == [(IssueCode.INVALID_LINE_SEPARATORS, "payload")]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("", id="empty"),
        pytest.param("\n".join(example_1_lines()[:30]), id="30 lines"),
    ],
)
def test_fewer_than_31_lines_is_rejected(payload: str) -> None:
    with pytest.raises(InvalidQrPayloadError) as raised:
        parse(payload)

    assert issue_codes(raised.value) == [(IssueCode.INVALID_LINE_COUNT, "payload")]


# --- Line rules ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line_number", "value", "expected"),
    [
        pytest.param(1, "SPX", (IssueCode.INVALID_HEADER, "header.qr_type"), id="QR type"),
        pytest.param(1, "spc", (IssueCode.INVALID_HEADER, "header.qr_type"), id="QR type case"),
        pytest.param(2, "0201", (IssueCode.INVALID_HEADER, "header.version"), id="version"),
        pytest.param(3, "2", (IssueCode.INVALID_HEADER, "header.coding"), id="coding"),
        pytest.param(31, "EPX", (IssueCode.MISSING_TRAILER, "trailer"), id="trailer"),
        pytest.param(31, "", (IssueCode.MISSING_TRAILER, "trailer"), id="trailer missing"),
        pytest.param(
            5, "K", (IssueCode.INVALID_ADDRESS_TYPE, "creditor.address_type"), id="creditor K"
        ),
        pytest.param(
            5, "", (IssueCode.INVALID_ADDRESS_TYPE, "creditor.address_type"), id="creditor empty"
        ),
        pytest.param(
            21, "K", (IssueCode.INVALID_ADDRESS_TYPE, "debtor.address_type"), id="debtor K"
        ),
        pytest.param(
            13,
            "Someone",
            (IssueCode.ULTIMATE_CREDITOR_NOT_EMPTY, "ultimate_creditor"),
            id="ultimate creditor filled",
        ),
        pytest.param(20, "USD", (IssueCode.INVALID_CURRENCY, "amount.currency"), id="USD"),
        pytest.param(20, "chf", (IssueCode.INVALID_CURRENCY, "amount.currency"), id="lowercase"),
        pytest.param(
            28, "QRX", (IssueCode.INVALID_REFERENCE_TYPE, "reference.type"), id="reference type"
        ),
    ],
)
def test_invalid_line_is_rejected(
    line_number: int, value: str, expected: tuple[IssueCode, str]
) -> None:
    with pytest.raises(InvalidQrPayloadError) as raised:
        parse(example_1_with(line_number, value))

    assert issue_codes(raised.value) == [expected]


@pytest.mark.parametrize(
    "amount",
    [
        pytest.param("50", id="no decimals"),
        pytest.param("50.0", id="one decimal"),
        pytest.param("50.000", id="three decimals"),
        pytest.param("50,00", id="comma separator"),
        pytest.param("050.00", id="leading zero"),
        pytest.param("1'000.00", id="thousands separator"),
        pytest.param(" 50.00", id="padded"),
        pytest.param("-1.00", id="negative"),
        pytest.param("1000000000.00", id="13 characters"),
        pytest.param("CHF 50.00", id="currency in amount"),
    ],
)
def test_invalid_amount_format_is_rejected(amount: str) -> None:
    with pytest.raises(InvalidQrPayloadError) as raised:
        parse(example_1_with(19, amount))

    assert issue_codes(raised.value) == [(IssueCode.INVALID_AMOUNT_FORMAT, "amount.value")]


@pytest.mark.parametrize("amount", ["0.50", "0.00", "1949.75", "999999999.99"])
def test_valid_amount_formats_are_parsed_exactly(amount: str) -> None:
    invoice = parse(example_1_with(19, amount))

    assert invoice.amount.value == Decimal(amount)


def test_reports_every_line_problem_at_once() -> None:
    lines = example_1_lines()
    lines[1] = "0100"
    lines[19] = "USD"
    lines[27] = "QRX"

    with pytest.raises(InvalidQrPayloadError) as raised:
        parse("\n".join(lines))

    assert issue_codes(raised.value) == [
        (IssueCode.INVALID_HEADER, "header.version"),
        (IssueCode.INVALID_CURRENCY, "amount.currency"),
        (IssueCode.INVALID_REFERENCE_TYPE, "reference.type"),
    ]
    assert all(issue.severity is IssueSeverity.ERROR for issue in raised.value.issues)


def test_payload_errors_are_invalid_invoice_errors() -> None:
    with pytest.raises(InvalidInvoiceError):
        parse(example_1_with(1, "SPX"))


# --- Optional lines and the split with validation ------------------------------------------------


def test_omitted_status_a_lines_are_accepted() -> None:
    payload = "\n".join(example_1_lines()[:31])  # ends with "EPD"

    invoice = parse(payload)

    assert invoice.billing_information is None
    assert invoice.alternative_procedures == ()


def test_separator_after_trailer_is_read_as_empty_billing_information() -> None:
    payload = "\n".join(example_1_lines()[:31]) + "\n"

    assert parse(payload).billing_information is None


def test_more_than_two_alternative_procedures_are_parsed_and_left_to_validation() -> None:
    payload = "\n".join([*example_1_lines()[:31], "", "AV1", "AV2", "AV3"])

    validated = ensure_valid(parse(payload))

    assert validated.alternative_procedures == ("AV1", "AV2", "AV3")
    assert [warning.code for warning in validated.warnings] == [
        IssueCode.INVALID_ALTERNATIVE_PROCEDURE
    ]


def test_incomplete_debtor_parses_and_fails_validation_on_the_missing_fields() -> None:
    lines = example_1_lines()
    lines[21] = ""  # debtor name
    lines[25] = ""  # debtor town

    with pytest.raises(InvalidInvoiceError) as raised:
        ensure_valid(parse("\n".join(lines)))

    assert issue_codes(raised.value) == [
        (IssueCode.FIELD_REQUIRED, "debtor.name"),
        (IssueCode.FIELD_REQUIRED, "debtor.address.town"),
    ]


def test_invalid_iban_parses_and_is_rejected_by_validation() -> None:
    invoice = parse(example_1_with(4, "CH6431961000004421558"))

    with pytest.raises(InvalidInvoiceError) as raised:
        ensure_valid(invoice)

    assert issue_codes(raised.value) == [(IssueCode.INVALID_IBAN, "creditor.iban")]
