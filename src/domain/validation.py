import re
from collections.abc import Iterator
from dataclasses import dataclass, replace
from decimal import Decimal

from src.domain.errors import InvalidInvoiceError
from src.domain.iso_countries import COUNTRY_CODES
from src.domain.models.amount import Amount, Currency
from src.domain.models.invoice import Invoice
from src.domain.models.party import Address
from src.domain.models.reference import Reference, ReferenceType
from src.domain.models.validation_issue import IssueCode, IssueSeverity, ValidationIssue

# CH/LI IBAN (ISO 13616): country, 2 check digits, 5-digit IID, 12 alphanumeric account chars.
_IBAN_PATTERN = re.compile(r"(CH|LI)[0-9]{7}[0-9A-Z]{12}")
_QR_IID_RANGE = range(30000, 32000)  # ch. 2.8
_QRR_PATTERN = re.compile(r"[0-9]{27}")
_QRR_CARRY_TABLE = (0, 9, 4, 6, 8, 2, 7, 1, 3, 5)  # Annex B
_SCOR_PATTERN = re.compile(r"RF[0-9]{2}[0-9A-Z]{1,21}")  # ch. 2.12.2: 5-25 chars

_CENT = Decimal("0.01")
_MIN_AMOUNT = Decimal("0.01")
_MAX_AMOUNT = Decimal("999999999.99")

_NAME_MAX = 70
_STREET_MAX = 70
_BUILDING_NUMBER_MAX = 16
_POSTAL_CODE_MAX = 16
_TOWN_MAX = 35
_ADDITIONAL_INFORMATION_MAX = 140
_ALTERNATIVE_PROCEDURES_MAX = 2
_ALTERNATIVE_PROCEDURE_MAX = 100

_ERROR = IssueSeverity.ERROR
_WARNING = IssueSeverity.WARNING


# --- Check-digit primitives ---------------------------------------------------------------------


def _iso7064_mod97(value: str) -> int:
    """ISO 7064 mod 97-10 remainder, shared by IBAN and SCOR. A valid value gives 1."""
    rearranged = value[4:] + value[:4]
    digits = "".join(str(int(char, 36)) for char in rearranged)  # 0-9 → 0-9, A-Z → 10-35
    return int(digits) % 97


def is_valid_iban(iban: str) -> bool:
    return _IBAN_PATTERN.fullmatch(iban) is not None and _iso7064_mod97(iban) == 1


def is_qr_iban(iban: str) -> bool:
    """IID (positions 5-9) in the QR-IID range (ch. 2.8, 2.10). Expects a valid IBAN."""
    return int(iban[4:9]) in _QR_IID_RANGE


def qrr_check_digit(first_26_digits: str) -> int:
    """Modulo 10 recursive (Annex B)."""
    carry = 0
    for digit in first_26_digits:
        carry = _QRR_CARRY_TABLE[(carry + int(digit)) % 10]
    return (10 - carry) % 10


def is_valid_qrr(reference: str) -> bool:
    """27 digits, not all zeros, last digit is the check digit (ch. 2.12.1)."""
    if _QRR_PATTERN.fullmatch(reference) is None or set(reference) == {"0"}:
        return False
    return qrr_check_digit(reference[:26]) == int(reference[26])


def is_valid_scor(reference: str) -> bool:
    """ISO 11649 creditor reference, compared case-insensitively (ch. 2.12.2)."""
    normalized = reference.upper()
    return _SCOR_PATTERN.fullmatch(normalized) is not None and _iso7064_mod97(normalized) == 1


# --- Invoice rules -------------------------------------------------------------------------------


def _issue(code: IssueCode, field: str, message: str, severity: IssueSeverity) -> ValidationIssue:
    return ValidationIssue(code=code, field=field, message=message, severity=severity)


def _creditor_account_issues(invoice: Invoice) -> list[ValidationIssue]:
    if is_valid_iban(invoice.creditor.iban):
        return []
    return [
        _issue(
            IssueCode.INVALID_IBAN,
            "creditor.iban",
            "IBAN must be a 21-character CH or LI IBAN with a valid check digit",
            _ERROR,
        )
    ]


def _combination_issues(invoice: Invoice) -> list[ValidationIssue]:
    """Account/reference/currency combinations (ch. 4.3.2, 7.1; v2.4 change control)."""
    iban = invoice.creditor.iban
    if not is_valid_iban(iban):
        return []  # already reported; QR-IBAN detection needs a valid IBAN

    uses_qr_iban = is_qr_iban(iban)
    uses_qrr = invoice.reference.type is ReferenceType.QRR
    if uses_qr_iban and not uses_qrr:
        return [
            _issue(
                IssueCode.QR_IBAN_REQUIRES_QRR,
                "reference.type",
                "A QR-IBAN can only be used with a QR reference (QRR)",
                _ERROR,
            )
        ]
    if uses_qrr and not uses_qr_iban:
        return [
            _issue(
                IssueCode.QRR_REQUIRES_QR_IBAN,
                "reference.type",
                "A QR reference (QRR) requires a QR-IBAN; use SCOR or NON with a regular IBAN",
                _ERROR,
            )
        ]
    if uses_qrr and invoice.amount.currency is Currency.EUR:
        # v2.4 allows QRR in CHF only, but v2.3 bills (same Version 0200) stay valid until Nov 2027.
        return [
            _issue(
                IssueCode.EUR_QR_REFERENCE_DEPRECATED,
                "amount.currency",
                "QR-IBAN with QR reference in EUR is no longer allowed from IG v2.4",
                _WARNING,
            )
        ]
    return []


def _reference_issues(invoice: Invoice) -> list[ValidationIssue]:
    reference = invoice.reference
    value = reference.value or ""
    match reference.type:
        case ReferenceType.QRR if not is_valid_qrr(value):
            return [
                _issue(
                    IssueCode.INVALID_QR_REFERENCE,
                    "reference.value",
                    "QR reference must be 27 digits, not all zeros, with a valid check digit",
                    _ERROR,
                )
            ]
        case ReferenceType.SCOR if not is_valid_scor(value):
            return [
                _issue(
                    IssueCode.INVALID_CREDITOR_REFERENCE,
                    "reference.value",
                    "Creditor reference must be 5-25 characters, start with RF and pass mod 97",
                    _ERROR,
                )
            ]
        case ReferenceType.NON if value:
            return [
                _issue(
                    IssueCode.REFERENCE_NOT_ALLOWED,
                    "reference.value",
                    "Reference must be empty when the reference type is NON",
                    _ERROR,
                )
            ]
    return []


def _amount_issues(invoice: Invoice) -> list[ValidationIssue]:
    value = invoice.amount.value
    if value is None:
        return []  # the debtor enters the amount
    if not value.is_finite():
        return [_amount_out_of_range()]
    issues = []
    if value != value.quantize(_CENT):
        issues.append(
            _issue(
                IssueCode.AMOUNT_PRECISION,
                "amount.value",
                "Amount must have at most 2 decimal places",
                _ERROR,
            )
        )
    is_allowed_zero = value == 0 and invoice.is_notification  # ch. 4.4
    if not is_allowed_zero and not _MIN_AMOUNT <= value <= _MAX_AMOUNT:
        issues.append(_amount_out_of_range())
    return issues


def _amount_out_of_range() -> ValidationIssue:
    return _issue(
        IssueCode.AMOUNT_OUT_OF_RANGE,
        "amount.value",
        "Amount must be between 0.01 and 999999999.99 (0.00 only for notifications)",
        _ERROR,
    )


def _length_issues(
    field: str, value: str | None, max_length: int, *, required: bool
) -> list[ValidationIssue]:
    if not value:
        if required:
            return [_issue(IssueCode.FIELD_REQUIRED, field, "Field is required", _ERROR)]
        return []
    if len(value) > max_length:
        return [
            _issue(
                IssueCode.FIELD_TOO_LONG,
                field,
                f"Field must be at most {max_length} characters",
                _ERROR,
            )
        ]
    return []


def _party_issues(prefix: str, name: str, address: Address) -> list[ValidationIssue]:
    """Name and structured address of the creditor or ultimate debtor (ch. 4.2.2)."""
    issues = [
        *_length_issues(f"{prefix}.name", name, _NAME_MAX, required=True),
        *_length_issues(f"{prefix}.address.street", address.street, _STREET_MAX, required=False),
        *_length_issues(
            f"{prefix}.address.building_number",
            address.building_number,
            _BUILDING_NUMBER_MAX,
            required=False,
        ),
        *_length_issues(
            f"{prefix}.address.postal_code", address.postal_code, _POSTAL_CODE_MAX, required=True
        ),
        *_length_issues(f"{prefix}.address.town", address.town, _TOWN_MAX, required=True),
    ]
    if address.country not in COUNTRY_CODES:
        issues.append(
            _issue(
                IssueCode.INVALID_COUNTRY,
                f"{prefix}.address.country",
                "Country must be an ISO 3166-1 alpha-2 code",
                _ERROR,
            )
        )
    if address.postal_code.upper().startswith(f"{address.country}-"):
        issues.append(
            _issue(
                IssueCode.POSTAL_CODE_COUNTRY_PREFIX,
                f"{prefix}.address.postal_code",
                "Postal code must not include a country prefix",
                _ERROR,
            )
        )
    return issues


def _parties_issues(invoice: Invoice) -> list[ValidationIssue]:
    creditor = invoice.creditor
    issues = _party_issues("creditor", creditor.name, creditor.address)
    if invoice.debtor is not None:
        issues += _party_issues("debtor", invoice.debtor.name, invoice.debtor.address)
    return issues


def _additional_information_issues(invoice: Invoice) -> list[ValidationIssue]:
    """Unstructured message (O) and billing information (A) (ch. 4.3.3, 4.2.1)."""
    message = invoice.unstructured_message
    issues = _length_issues(
        "unstructured_message", message, _ADDITIONAL_INFORMATION_MAX, required=False
    )
    billing = invoice.billing_information
    if not billing:
        return issues
    if len(billing) > _ADDITIONAL_INFORMATION_MAX or not billing.startswith("//"):
        issues.append(
            _issue(
                IssueCode.INVALID_BILLING_INFORMATION,
                "billing_information",
                "Billing information must start with // and be at most 140 characters",
                _WARNING,
            )
        )
    # Only the status A line can push the total over the limit, so this must not reject the bill.
    if len(message or "") + len(billing) > _ADDITIONAL_INFORMATION_MAX:
        issues.append(
            _issue(
                IssueCode.ADDITIONAL_INFORMATION_TOO_LONG,
                "billing_information",
                "Unstructured message and billing information together exceed 140 characters",
                _WARNING,
            )
        )
    return issues


def _alternative_procedure_issues(invoice: Invoice) -> list[ValidationIssue]:
    """Status A lines: problems are warnings, never errors (ch. 4.2.1)."""
    procedures = invoice.alternative_procedures
    issues = []
    if len(procedures) > _ALTERNATIVE_PROCEDURES_MAX:
        issues.append(
            _issue(
                IssueCode.INVALID_ALTERNATIVE_PROCEDURE,
                "alternative_procedures",
                "At most 2 alternative procedures are allowed",
                _WARNING,
            )
        )
    for index, procedure in enumerate(procedures):
        if len(procedure) > _ALTERNATIVE_PROCEDURE_MAX:
            issues.append(
                _issue(
                    IssueCode.INVALID_ALTERNATIVE_PROCEDURE,
                    f"alternative_procedures[{index}]",
                    "Alternative procedure must be at most 100 characters",
                    _WARNING,
                )
            )
    return issues


@dataclass(frozen=True, slots=True)
class _TextField:
    path: str
    value: str
    severity: IssueSeverity  # status A fields only produce warnings (ch. 4.2.1)


def _text_fields(invoice: Invoice) -> Iterator[_TextField]:
    def party(prefix: str, name: str, address: Address) -> Iterator[_TextField]:
        yield _TextField(f"{prefix}.name", name, _ERROR)
        for attribute in ("street", "building_number", "postal_code", "town", "country"):
            value: str | None = getattr(address, attribute)
            if value is not None:
                yield _TextField(f"{prefix}.address.{attribute}", value, _ERROR)

    yield from party("creditor", invoice.creditor.name, invoice.creditor.address)
    if invoice.debtor is not None:
        yield from party("debtor", invoice.debtor.name, invoice.debtor.address)
    if invoice.reference.value is not None:
        yield _TextField("reference.value", invoice.reference.value, _ERROR)
    if invoice.unstructured_message is not None:
        yield _TextField("unstructured_message", invoice.unstructured_message, _ERROR)
    if invoice.billing_information is not None:
        yield _TextField("billing_information", invoice.billing_information, _WARNING)
    for index, procedure in enumerate(invoice.alternative_procedures):
        yield _TextField(f"alternative_procedures[{index}]", procedure, _WARNING)


def _is_allowed_character(char: str) -> bool:
    """Character set of ch. 4.1: Basic Latin, Latin-1 Supplement, Latin Extended-A, Ș ș Ț ț, €."""
    code_point = ord(char)
    return (
        0x20 <= code_point <= 0x7E
        or 0xA0 <= code_point <= 0x17F
        or 0x218 <= code_point <= 0x21B
        or code_point == 0x20AC
    )


def _text_issues(invoice: Invoice) -> list[ValidationIssue]:
    issues = []
    for field in _text_fields(invoice):
        if not all(_is_allowed_character(char) for char in field.value):
            issues.append(
                _issue(
                    IssueCode.INVALID_CHARACTERS,
                    field.path,
                    "Field contains characters outside the QR-bill character set",
                    field.severity,
                )
            )
        if field.value != field.value.strip():
            issues.append(
                _issue(
                    IssueCode.PADDED_VALUE,
                    field.path,
                    "Field must not have leading or trailing blanks",
                    field.severity,
                )
            )
    return issues


_RULES = (
    _creditor_account_issues,
    _combination_issues,
    _reference_issues,
    _amount_issues,
    _parties_issues,
    _additional_information_issues,
    _alternative_procedure_issues,
    _text_issues,
)


def validate_invoice(invoice: Invoice) -> tuple[ValidationIssue, ...]:
    """Run every rule and return all issues, errors and warnings alike."""
    return tuple(issue for rule in _RULES for issue in rule(invoice))


def ensure_valid(invoice: Invoice) -> Invoice:
    """Raise `InvalidInvoiceError` with all errors, or return the invoice in canonical form.

    The returned invoice has its amount quantized to cents, a SCOR reference in upper case, and
    the new warnings merged with any it already carried (e.g. from the parser), without
    duplicates, so calling this twice is safe.
    """
    issues = validate_invoice(invoice)
    errors = tuple(issue for issue in issues if issue.severity is IssueSeverity.ERROR)
    if errors:
        raise InvalidInvoiceError(errors)
    new_warnings = (issue for issue in issues if issue.severity is IssueSeverity.WARNING)
    return replace(
        invoice,
        amount=_quantized_to_cents(invoice.amount),
        reference=_canonical_reference(invoice.reference),
        warnings=tuple(dict.fromkeys((*invoice.warnings, *new_warnings))),  # ordered dedup
    )


def _canonical_reference(reference: Reference) -> Reference:
    """SCOR is case-insensitive (ISO 11649): store one form, so equal references compare equal."""
    if reference.type is ReferenceType.SCOR and reference.value is not None:
        return replace(reference, value=reference.value.upper())
    return reference


def _quantized_to_cents(amount: Amount) -> Amount:
    """Canonical money form: always 2 decimal places, so 50 and 50.000 are stored as 50.00.

    Only called after validation, so the value is finite and has no more than 2 decimal places:
    quantizing never rounds.
    """
    if amount.value is None:
        return amount
    return replace(amount, value=amount.value.quantize(_CENT))
