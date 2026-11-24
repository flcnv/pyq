from collections.abc import Iterable

from src.domain.models.invoice import Invoice
from src.domain.models.reference import ReferenceType
from src.domain.models.validation_issue import IssueCode, IssueSeverity, ValidationIssue

# A structured reference identifies one bill of one creditor. Without one (NON), an IBAN and an
# amount match unrelated bills, e.g. a monthly fee, so those are never compared.
_IDENTIFYING_REFERENCES = frozenset({ReferenceType.QRR, ReferenceType.SCOR})


def can_identify_payment(invoice: Invoice) -> bool:
    """Notifications ("DO NOT USE FOR PAYMENT") are never paid, so they can't be paid twice."""
    return invoice.reference.type in _IDENTIFYING_REFERENCES and not invoice.is_notification


def possible_duplicate_warning(
    invoice: Invoice, same_payment: Iterable[Invoice]
) -> ValidationIssue | None:
    """A warning naming the stored invoices that would pay the same bill, or None.

    `same_payment` holds stored invoices with the same account, reference, currency and amount.
    Never an error: a second copy may be legitimate (a reminder), but it must not be paid twice.
    """
    if not can_identify_payment(invoice):
        return None
    matches = tuple(stored.id for stored in same_payment if can_identify_payment(stored))
    if not matches:
        return None
    return ValidationIssue(
        code=IssueCode.POSSIBLE_DUPLICATE,
        field="reference.value",
        message=(
            "Possible duplicate: same creditor account, reference, currency and amount as "
            f"invoice(s) {', '.join(str(invoice_id) for invoice_id in matches)}"
        ),
        severity=IssueSeverity.WARNING,
        related_invoice_ids=matches,
    )
