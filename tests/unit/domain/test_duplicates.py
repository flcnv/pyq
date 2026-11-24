from dataclasses import replace
from decimal import Decimal

import pytest

from src.domain.duplicates import can_identify_payment, possible_duplicate_warning
from src.domain.models.invoice import Invoice
from src.domain.models.reference import ReferenceType
from src.domain.models.validation_issue import IssueCode, IssueSeverity
from tests.unit.domain.factories import CREDITOR_REFERENCE, IBAN, make_invoice

QRR_BILL = make_invoice()
SCOR_BILL = make_invoice(
    iban=IBAN, reference_type=ReferenceType.SCOR, reference_value=CREDITOR_REFERENCE
)
NON_BILL = make_invoice(iban=IBAN, reference_type=ReferenceType.NON, reference_value=None)
NOTIFICATION = make_invoice(amount=Decimal("0.00"), unstructured_message="DO NOT USE FOR PAYMENT")


@pytest.mark.parametrize(
    ("invoice", "expected"),
    [
        (QRR_BILL, True),
        (SCOR_BILL, True),
        (NON_BILL, False),  # an IBAN and an amount alone don't identify a bill
        (NOTIFICATION, False),  # never paid
    ],
    ids=["QRR", "SCOR", "NON", "notification"],
)
def test_only_bills_with_a_structured_reference_can_be_compared(
    invoice: Invoice, expected: bool
) -> None:
    assert can_identify_payment(invoice) is expected


def test_warning_names_every_matching_invoice() -> None:
    first, second = make_invoice(), make_invoice()

    warning = possible_duplicate_warning(QRR_BILL, [first, second])

    assert warning is not None
    assert warning.code is IssueCode.POSSIBLE_DUPLICATE
    assert warning.severity is IssueSeverity.WARNING
    assert warning.related_invoice_ids == (first.id, second.id)
    assert str(first.id) in warning.message


def test_no_matches_means_no_warning() -> None:
    assert possible_duplicate_warning(QRR_BILL, []) is None


def test_stored_notifications_are_not_reported_as_duplicates() -> None:
    zero_amount_bill = replace(QRR_BILL, amount=NOTIFICATION.amount, unstructured_message="Rent")

    assert possible_duplicate_warning(zero_amount_bill, [NOTIFICATION]) is None


@pytest.mark.parametrize("invoice", [NON_BILL, NOTIFICATION], ids=["NON", "notification"])
def test_bills_that_cant_be_identified_get_no_warning(invoice: Invoice) -> None:
    assert possible_duplicate_warning(invoice, [make_invoice()]) is None
