import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from uuid import UUID

from src.domain.errors import InvalidQrPayloadError
from src.domain.models.amount import Amount, Currency
from src.domain.models.invoice import Invoice, InvoiceSource
from src.domain.models.party import Address, Creditor, Debtor
from src.domain.models.reference import Reference, ReferenceType
from src.domain.models.validation_issue import IssueCode, IssueSeverity, ValidationIssue

# Only payload *text* rules live here. Rules that hold for any Invoice (IBAN, references, lengths,
# character set, ...) live in validation.py, so QR and LLM output are checked the same way.

MAX_PAYLOAD_LENGTH = 997  # ch. 4.1, separators included
MANDATORY_LINE_COUNT = 31  # lines 1-31 (status M/D/O/X) up to and including the trailer

_QR_TYPE = "SPC"
_VERSION = "0200"
_CODING = "1"
_TRAILER = "EPD"
_STRUCTURED_ADDRESS = "S"
# Exactly 2 decimals, no leading zeros, at most 12 characters (9 + "." + 2). ch. 4.2.2
_AMOUNT_PATTERN = re.compile(r"(0|[1-9][0-9]{0,8})\.[0-9]{2}")

# 0-based line indexes (the spec numbers lines from 1).
_QR_TYPE_LINE = 0
_VERSION_LINE = 1
_CODING_LINE = 2
_IBAN_LINE = 3
_CREDITOR_LINES = slice(4, 11)
_ULTIMATE_CREDITOR_LINES = slice(11, 18)
_AMOUNT_LINE = 18
_CURRENCY_LINE = 19
_DEBTOR_LINES = slice(20, 27)
_REFERENCE_TYPE_LINE = 27
_REFERENCE_LINE = 28
_UNSTRUCTURED_MESSAGE_LINE = 29
_TRAILER_LINE = 30
_BILLING_INFORMATION_LINE = 31
_FIRST_ALTERNATIVE_PROCEDURE_LINE = 32


@dataclass(frozen=True, slots=True)
class _AddressLines:
    """The 7 lines of an address group: AdrTp, Name, StrtNm, BldgNb, PstCd, TwnNm, Ctry."""

    address_type: str
    name: str
    street: str
    building_number: str
    postal_code: str
    town: str
    country: str

    @classmethod
    def from_lines(cls, lines: list[str]) -> _AddressLines:
        return cls(*lines)

    def is_empty(self) -> bool:
        return not any(
            (
                self.address_type,
                self.name,
                self.street,
                self.building_number,
                self.postal_code,
                self.town,
                self.country,
            )
        )

    def to_address(self) -> Address:
        return Address(
            street=self.street or None,
            building_number=self.building_number or None,
            postal_code=self.postal_code,
            town=self.town,
            country=self.country,
        )


def parse_qr_payload(payload: str, *, invoice_id: UUID, created_at: datetime) -> Invoice:
    """Parse a Swiss QR Code payload into an `Invoice`, checking the payload text rules.

    Raises `InvalidQrPayloadError` with every text problem found. Run `ensure_valid` on the result
    for the remaining rules. `invoice_id` and `created_at` are passed in to keep parsing pure.
    """
    issues = _envelope_issues(payload)
    if issues:
        raise InvalidQrPayloadError(issues)  # line positions are unreliable: stop here

    lines = _split_lines(payload)
    if len(lines) < MANDATORY_LINE_COUNT:
        raise InvalidQrPayloadError(
            (
                _error(
                    IssueCode.INVALID_LINE_COUNT,
                    "payload",
                    f"Payload must have at least {MANDATORY_LINE_COUNT} lines, got {len(lines)}",
                ),
            )
        )

    issues = _line_issues(lines)
    if issues:
        raise InvalidQrPayloadError(issues)
    return _build_invoice(lines, invoice_id=invoice_id, created_at=created_at)


# --- Envelope: length and separators -------------------------------------------------------------


def _envelope_issues(payload: str) -> tuple[ValidationIssue, ...]:
    issues = []
    if len(payload) > MAX_PAYLOAD_LENGTH:
        issues.append(
            _error(
                IssueCode.PAYLOAD_TOO_LONG,
                "payload",
                f"Payload must be at most {MAX_PAYLOAD_LENGTH} characters, got {len(payload)}",
            )
        )
    if not _has_consistent_separators(payload):
        issues.append(
            _error(
                IssueCode.INVALID_LINE_SEPARATORS,
                "payload",
                "Lines must be separated by CRLF or LF, the same throughout the payload",
            )
        )
    return tuple(issues)


def _has_consistent_separators(payload: str) -> bool:
    """CRLF or LF, never mixed, no bare CR (ch. 4.1)."""
    without_crlf = payload.replace("\r\n", "")
    if "\r" in without_crlf:
        return False  # bare CR
    uses_crlf = "\r\n" in payload
    uses_lf = "\n" in without_crlf
    return not (uses_crlf and uses_lf)


def _split_lines(payload: str) -> list[str]:
    separator = "\r\n" if "\r\n" in payload else "\n"
    return payload.split(separator)


# --- Line rules ----------------------------------------------------------------------------------


def _line_issues(lines: list[str]) -> tuple[ValidationIssue, ...]:
    return (
        *_header_issues(lines),
        *_trailer_issues(lines),
        *_creditor_issues(lines),
        *_ultimate_creditor_issues(lines),
        *_amount_issues(lines),
        *_debtor_issues(lines),
        *_reference_type_issues(lines),
    )


def _header_issues(lines: list[str]) -> list[ValidationIssue]:
    expected = (
        (_QR_TYPE_LINE, "qr_type", _QR_TYPE),
        (_VERSION_LINE, "version", _VERSION),
        (_CODING_LINE, "coding", _CODING),
    )
    return [
        _error(
            IssueCode.INVALID_HEADER,
            f"header.{field}",
            f"Line {index + 1} ({field}) must be {value!r}",
        )
        for index, field, value in expected
        if lines[index] != value
    ]


def _trailer_issues(lines: list[str]) -> list[ValidationIssue]:
    if lines[_TRAILER_LINE] == _TRAILER:
        return []
    return [
        _error(
            IssueCode.MISSING_TRAILER,
            "trailer",
            f"Line {_TRAILER_LINE + 1} must be the trailer {_TRAILER!r}",
        )
    ]


def _creditor_issues(lines: list[str]) -> list[ValidationIssue]:
    creditor = _AddressLines.from_lines(lines[_CREDITOR_LINES])
    if creditor.address_type == _STRUCTURED_ADDRESS:
        return []
    return [_invalid_address_type("creditor")]


def _ultimate_creditor_issues(lines: list[str]) -> list[ValidationIssue]:
    """Reserved for future use: the 7 lines must be present but empty (status X)."""
    if _AddressLines.from_lines(lines[_ULTIMATE_CREDITOR_LINES]).is_empty():
        return []
    return [
        _error(
            IssueCode.ULTIMATE_CREDITOR_NOT_EMPTY,
            "ultimate_creditor",
            "Ultimate creditor lines (12-18) are reserved and must be empty",
        )
    ]


def _debtor_issues(lines: list[str]) -> list[ValidationIssue]:
    """The group is optional; if used, AdrTp must be S. Missing fields are checked in validation."""
    debtor = _AddressLines.from_lines(lines[_DEBTOR_LINES])
    if debtor.is_empty() or debtor.address_type == _STRUCTURED_ADDRESS:
        return []
    return [_invalid_address_type("debtor")]


def _invalid_address_type(party: str) -> ValidationIssue:
    # "K" (combined address) was dropped in v2.4; only structured addresses are accepted.
    return _error(
        IssueCode.INVALID_ADDRESS_TYPE,
        f"{party}.address_type",
        f"Address type must be {_STRUCTURED_ADDRESS!r} (structured address)",
    )


def _amount_issues(lines: list[str]) -> list[ValidationIssue]:
    issues = []
    amount = lines[_AMOUNT_LINE]
    if amount and _AMOUNT_PATTERN.fullmatch(amount) is None:
        issues.append(
            _error(
                IssueCode.INVALID_AMOUNT_FORMAT,
                "amount.value",
                "Amount must use '.' as separator, exactly 2 decimals and no leading zeros",
            )
        )
    if lines[_CURRENCY_LINE] not in Currency:
        issues.append(
            _error(IssueCode.INVALID_CURRENCY, "amount.currency", "Currency must be CHF or EUR")
        )
    return issues


def _reference_type_issues(lines: list[str]) -> list[ValidationIssue]:
    if lines[_REFERENCE_TYPE_LINE] in ReferenceType:
        return []
    return [
        _error(
            IssueCode.INVALID_REFERENCE_TYPE,
            "reference.type",
            "Reference type must be QRR, SCOR or NON",
        )
    ]


def _error(code: IssueCode, field: str, message: str) -> ValidationIssue:
    return ValidationIssue(code=code, field=field, message=message, severity=IssueSeverity.ERROR)


# --- Mapping to the domain -----------------------------------------------------------------------


def _build_invoice(lines: list[str], *, invoice_id: UUID, created_at: datetime) -> Invoice:
    """Map checked lines to domain objects: empty optional lines become None."""
    creditor = _AddressLines.from_lines(lines[_CREDITOR_LINES])
    debtor = _AddressLines.from_lines(lines[_DEBTOR_LINES])
    amount = lines[_AMOUNT_LINE]
    return Invoice(
        id=invoice_id,
        created_at=created_at,
        source=InvoiceSource.QR,
        creditor=Creditor(
            iban=lines[_IBAN_LINE], name=creditor.name, address=creditor.to_address()
        ),
        amount=Amount(
            value=Decimal(amount) if amount else None,
            currency=Currency(lines[_CURRENCY_LINE]),
        ),
        debtor=None if debtor.is_empty() else Debtor(name=debtor.name, address=debtor.to_address()),
        reference=Reference(
            type=ReferenceType(lines[_REFERENCE_TYPE_LINE]),
            value=lines[_REFERENCE_LINE] or None,
        ),
        unstructured_message=lines[_UNSTRUCTURED_MESSAGE_LINE] or None,
        billing_information=_optional_line(lines, _BILLING_INFORMATION_LINE),
        alternative_procedures=_alternative_procedures(lines),
    )


def _optional_line(lines: list[str], index: int) -> str | None:
    """Status A lines may be omitted or present but empty (the spec's own examples do both)."""
    if index >= len(lines):
        return None
    return lines[index] or None


def _alternative_procedures(lines: list[str]) -> tuple[str, ...]:
    """All non-empty lines after billing information. More than 2 is a warning in validation."""
    return tuple(line for line in lines[_FIRST_ALTERNATIVE_PROCEDURE_LINE:] if line)
