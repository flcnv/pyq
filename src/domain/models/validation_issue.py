from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID


class IssueCode(StrEnum):
    """Stable, machine-readable codes. They are part of the API contract: don't rename them."""

    INVALID_IBAN = "invalid_iban"
    QR_IBAN_REQUIRES_QRR = "qr_iban_requires_qrr"
    QRR_REQUIRES_QR_IBAN = "qrr_requires_qr_iban"
    EUR_QR_REFERENCE_DEPRECATED = "eur_qr_reference_deprecated"
    INVALID_QR_REFERENCE = "invalid_qr_reference"
    INVALID_CREDITOR_REFERENCE = "invalid_creditor_reference"
    REFERENCE_NOT_ALLOWED = "reference_not_allowed"
    AMOUNT_OUT_OF_RANGE = "amount_out_of_range"
    AMOUNT_PRECISION = "amount_precision"
    FIELD_REQUIRED = "field_required"
    FIELD_TOO_LONG = "field_too_long"
    INVALID_COUNTRY = "invalid_country"
    POSTAL_CODE_COUNTRY_PREFIX = "postal_code_country_prefix"
    INVALID_BILLING_INFORMATION = "invalid_billing_information"
    ADDITIONAL_INFORMATION_TOO_LONG = "additional_information_too_long"
    INVALID_ALTERNATIVE_PROCEDURE = "invalid_alternative_procedure"
    INVALID_CHARACTERS = "invalid_characters"
    PADDED_VALUE = "padded_value"
    POSSIBLE_DUPLICATE = "possible_duplicate"
    # Payload text rules (qr_payload.py)
    PAYLOAD_TOO_LONG = "payload_too_long"
    INVALID_LINE_SEPARATORS = "invalid_line_separators"
    INVALID_LINE_COUNT = "invalid_line_count"
    INVALID_HEADER = "invalid_header"
    MISSING_TRAILER = "missing_trailer"
    ULTIMATE_CREDITOR_NOT_EMPTY = "ultimate_creditor_not_empty"
    INVALID_ADDRESS_TYPE = "invalid_address_type"
    INVALID_AMOUNT_FORMAT = "invalid_amount_format"
    INVALID_CURRENCY = "invalid_currency"
    INVALID_REFERENCE_TYPE = "invalid_reference_type"


class IssueSeverity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True, slots=True, kw_only=True)
class ValidationIssue:
    code: IssueCode
    field: str
    message: str
    severity: IssueSeverity
    related_invoice_ids: tuple[UUID, ...] = ()  # other invoices the issue is about
