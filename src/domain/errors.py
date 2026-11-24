from src.domain.models.validation_issue import ValidationIssue


class DomainError(Exception):
    """Base class for all domain errors."""


class InvalidInvoiceError(DomainError):
    summary = "Invoice failed validation"

    def __init__(self, issues: tuple[ValidationIssue, ...]) -> None:
        super().__init__(f"{self.summary} with {len(issues)} error(s)")
        self.issues = issues


class InvalidQrPayloadError(InvalidInvoiceError):
    """The QR payload text breaks the spec's format rules, so no `Invoice` could be built.

    A subclass of `InvalidInvoiceError`: callers that reject invalid invoices reject these too.
    """

    summary = "QR payload is malformed"
