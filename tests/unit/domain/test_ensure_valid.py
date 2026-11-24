from dataclasses import replace
from decimal import Decimal

import pytest

from src.domain.errors import InvalidInvoiceError
from src.domain.models.amount import Currency
from src.domain.models.reference import ReferenceType
from src.domain.models.validation_issue import IssueCode, IssueSeverity, ValidationIssue
from src.domain.validation import ensure_valid
from tests.unit.domain.factories import IBAN, make_invoice


def test_reports_every_error_at_once() -> None:
    invoice = make_invoice(
        iban="CH6431961000004421558",
        amount=Decimal("1000000000.00"),
        creditor_name="",
    )

    with pytest.raises(InvalidInvoiceError) as raised:
        ensure_valid(invoice)

    assert {issue.code for issue in raised.value.issues} == {
        IssueCode.INVALID_IBAN,
        IssueCode.AMOUNT_OUT_OF_RANGE,
        IssueCode.FIELD_REQUIRED,
    }
    assert all(issue.severity is IssueSeverity.ERROR for issue in raised.value.issues)


def test_errors_reject_the_invoice_even_with_warnings_present() -> None:
    invoice = make_invoice(amount=Decimal("-1.00"), alternative_procedures=("A1", "A2", "A3"))

    with pytest.raises(InvalidInvoiceError) as raised:
        ensure_valid(invoice)

    assert [issue.code for issue in raised.value.issues] == [IssueCode.AMOUNT_OUT_OF_RANGE]


def test_attaches_warnings_without_rejecting() -> None:
    invoice = make_invoice(currency=Currency.EUR)

    validated = ensure_valid(invoice)

    assert [warning.code for warning in validated.warnings] == [
        IssueCode.EUR_QR_REFERENCE_DEPRECATED
    ]


def test_valid_invoice_comes_back_unchanged() -> None:
    invoice = make_invoice()

    assert ensure_valid(invoice) == invoice
    assert ensure_valid(invoice).warnings == ()


def test_is_idempotent() -> None:
    once = ensure_valid(make_invoice(currency=Currency.EUR))

    assert ensure_valid(once) == once


@pytest.mark.parametrize(
    "amount",
    [
        pytest.param("50", id="no decimals"),
        pytest.param("50.0", id="one decimal"),
        pytest.param("50.000", id="extra zero"),
    ],
)
def test_returns_amount_quantized_to_cents(amount: str) -> None:
    validated = ensure_valid(make_invoice(amount=Decimal(amount)))

    assert validated.amount.value is not None
    assert str(validated.amount.value) == "50.00"


def test_keeps_a_missing_amount_missing() -> None:
    assert ensure_valid(make_invoice(amount=None)).amount.value is None


PARSER_WARNING = ValidationIssue(
    code=IssueCode.INVALID_ALTERNATIVE_PROCEDURE,
    field="alternative_procedures[2]",
    message="Dropped a third alternative procedure line",
    severity=IssueSeverity.WARNING,
)


def test_keeps_warnings_the_invoice_already_carries() -> None:
    invoice = replace(make_invoice(currency=Currency.EUR), warnings=(PARSER_WARNING,))

    validated = ensure_valid(invoice)

    assert [warning.code for warning in validated.warnings] == [
        IssueCode.INVALID_ALTERNATIVE_PROCEDURE,
        IssueCode.EUR_QR_REFERENCE_DEPRECATED,
    ]


def test_scor_reference_is_stored_in_upper_case() -> None:
    # ISO 11649 is case-insensitive: one stored form, so equal references compare equal.
    invoice = make_invoice(
        iban=IBAN, reference_type=ReferenceType.SCOR, reference_value="rf18539007547034"
    )

    assert ensure_valid(invoice).reference.value == "RF18539007547034"


def test_qr_reference_is_left_as_is() -> None:
    assert ensure_valid(make_invoice()).reference == make_invoice().reference
