from dataclasses import replace
from decimal import Decimal

import pytest

from src.domain.models.amount import Currency
from src.domain.models.invoice import Invoice
from src.domain.models.party import Debtor
from src.domain.models.reference import ReferenceType
from src.domain.models.validation_issue import IssueCode, IssueSeverity
from src.domain.validation import validate_invoice
from tests.unit.domain.factories import (
    CREDITOR_ADDRESS,
    CREDITOR_REFERENCE,
    DEBTOR,
    IBAN,
    QR_IBAN,
    make_invoice,
)

ERROR = IssueSeverity.ERROR
WARNING = IssueSeverity.WARNING


def issues_of(invoice: Invoice) -> set[tuple[IssueCode, str, IssueSeverity]]:
    return {(issue.code, issue.field, issue.severity) for issue in validate_invoice(invoice)}


@pytest.mark.parametrize(
    "invoice",
    [
        pytest.param(make_invoice(), id="annex A example 1: QR-IBAN + QRR"),
        pytest.param(
            make_invoice(
                iban=IBAN,
                reference_type=ReferenceType.SCOR,
                reference_value=CREDITOR_REFERENCE,
            ),
            id="IBAN + SCOR",
        ),
        pytest.param(
            make_invoice(iban=IBAN, reference_type=ReferenceType.NON, reference_value=None),
            id="IBAN + NON",
        ),
        pytest.param(
            make_invoice(
                iban=IBAN,
                currency=Currency.EUR,
                reference_type=ReferenceType.SCOR,
                reference_value=CREDITOR_REFERENCE,
            ),
            id="EUR with IBAN + SCOR",
        ),
        pytest.param(make_invoice(debtor=None), id="no debtor"),
        pytest.param(make_invoice(amount=None), id="amount left to the debtor"),
        pytest.param(
            make_invoice(amount=Decimal("0.00"), unstructured_message="DO NOT USE FOR PAYMENT"),
            id="notification with 0.00",
        ),
        pytest.param(make_invoice(amount=Decimal("0.01")), id="minimum amount"),
        pytest.param(make_invoice(amount=Decimal("999999999.99")), id="maximum amount"),
        pytest.param(make_invoice(amount=Decimal("50.000")), id="extra zero, same value"),
        pytest.param(
            make_invoice(billing_information="//S1/10/10201409/11/190512/20/1400.000-53"),
            id="billing information",
        ),
        pytest.param(
            make_invoice(alternative_procedures=("Name AV1: UV;UltraPay005;12345", "XY;XYService")),
            id="two alternative procedures",
        ),
        pytest.param(
            make_invoice(unstructured_message="Payment € Ș ș Ț ț ÿ Ž"), id="allowed characters"
        ),
    ],
)
def test_valid_invoices_have_no_issues(invoice: Invoice) -> None:
    assert validate_invoice(invoice) == ()


def test_invalid_iban_is_reported_once_without_combination_issues() -> None:
    invoice = make_invoice(iban="CH6431961000004421558")

    assert issues_of(invoice) == {(IssueCode.INVALID_IBAN, "creditor.iban", ERROR)}


@pytest.mark.parametrize(
    ("iban", "reference_type", "reference_value", "currency", "expected"),
    [
        pytest.param(
            QR_IBAN,
            ReferenceType.SCOR,
            CREDITOR_REFERENCE,
            Currency.CHF,
            (IssueCode.QR_IBAN_REQUIRES_QRR, "reference.type", ERROR),
            id="QR-IBAN + SCOR",
        ),
        pytest.param(
            QR_IBAN,
            ReferenceType.NON,
            None,
            Currency.CHF,
            (IssueCode.QR_IBAN_REQUIRES_QRR, "reference.type", ERROR),
            id="QR-IBAN + NON",
        ),
        pytest.param(
            IBAN,
            ReferenceType.QRR,
            "000008207791225857421286694",
            Currency.CHF,
            (IssueCode.QRR_REQUIRES_QR_IBAN, "reference.type", ERROR),
            id="IBAN + QRR",
        ),
        pytest.param(
            QR_IBAN,
            ReferenceType.QRR,
            "000008207791225857421286694",
            Currency.EUR,
            (IssueCode.EUR_QR_REFERENCE_DEPRECATED, "amount.currency", WARNING),
            id="QR-IBAN + QRR in EUR (v2.3 transition)",
        ),
    ],
)
def test_account_reference_currency_combinations(
    iban: str,
    reference_type: ReferenceType,
    reference_value: str | None,
    currency: Currency,
    expected: tuple[IssueCode, str, IssueSeverity],
) -> None:
    invoice = make_invoice(
        iban=iban,
        reference_type=reference_type,
        reference_value=reference_value,
        currency=currency,
    )

    assert issues_of(invoice) == {expected}


@pytest.mark.parametrize(
    ("iban", "reference_type", "reference_value", "expected_code"),
    [
        pytest.param(
            QR_IBAN,
            ReferenceType.QRR,
            "000008207791225857421286695",
            IssueCode.INVALID_QR_REFERENCE,
            id="QRR with wrong check digit",
        ),
        pytest.param(
            QR_IBAN, ReferenceType.QRR, None, IssueCode.INVALID_QR_REFERENCE, id="QRR missing"
        ),
        pytest.param(
            IBAN,
            ReferenceType.SCOR,
            "RF720191230100405JSH0438",
            IssueCode.INVALID_CREDITOR_REFERENCE,
            id="SCOR from annex A example 4",
        ),
        pytest.param(
            IBAN, ReferenceType.SCOR, None, IssueCode.INVALID_CREDITOR_REFERENCE, id="SCOR missing"
        ),
        pytest.param(
            IBAN,
            ReferenceType.NON,
            CREDITOR_REFERENCE,
            IssueCode.REFERENCE_NOT_ALLOWED,
            id="NON with a reference",
        ),
    ],
)
def test_reference_must_match_its_type(
    iban: str,
    reference_type: ReferenceType,
    reference_value: str | None,
    expected_code: IssueCode,
) -> None:
    invoice = make_invoice(
        iban=iban, reference_type=reference_type, reference_value=reference_value
    )

    assert issues_of(invoice) == {(expected_code, "reference.value", ERROR)}


@pytest.mark.parametrize(
    ("amount", "expected_codes"),
    [
        pytest.param("0.00", {IssueCode.AMOUNT_OUT_OF_RANGE}, id="0.00 without notification"),
        pytest.param("-1.00", {IssueCode.AMOUNT_OUT_OF_RANGE}, id="negative"),
        pytest.param("1000000000.00", {IssueCode.AMOUNT_OUT_OF_RANGE}, id="above maximum"),
        pytest.param("50.001", {IssueCode.AMOUNT_PRECISION}, id="3 decimal places"),
        pytest.param(
            "0.001",
            {IssueCode.AMOUNT_PRECISION, IssueCode.AMOUNT_OUT_OF_RANGE},
            id="below minimum and too precise",
        ),
        pytest.param("NaN", {IssueCode.AMOUNT_OUT_OF_RANGE}, id="NaN"),
        pytest.param("Infinity", {IssueCode.AMOUNT_OUT_OF_RANGE}, id="infinity"),
    ],
)
def test_invalid_amounts(amount: str, expected_codes: set[IssueCode]) -> None:
    invoice = make_invoice(amount=Decimal(amount))

    assert issues_of(invoice) == {(code, "amount.value", ERROR) for code in expected_codes}


PARTY_CASES = [
    pytest.param({"name": ""}, IssueCode.FIELD_REQUIRED, "name", id="name missing"),
    pytest.param({"name": "N" * 71}, IssueCode.FIELD_TOO_LONG, "name", id="name 71 chars"),
    pytest.param(
        {"street": "S" * 71}, IssueCode.FIELD_TOO_LONG, "address.street", id="street 71 chars"
    ),
    pytest.param(
        {"building_number": "1" * 17},
        IssueCode.FIELD_TOO_LONG,
        "address.building_number",
        id="building number 17 chars",
    ),
    pytest.param(
        {"postal_code": ""},
        IssueCode.FIELD_REQUIRED,
        "address.postal_code",
        id="postal code missing",
    ),
    pytest.param(
        {"postal_code": "8" * 17},
        IssueCode.FIELD_TOO_LONG,
        "address.postal_code",
        id="postal code 17 chars",
    ),
    pytest.param(
        {"postal_code": "CH-8000"},
        IssueCode.POSTAL_CODE_COUNTRY_PREFIX,
        "address.postal_code",
        id="postal code with country prefix",
    ),
    pytest.param({"town": ""}, IssueCode.FIELD_REQUIRED, "address.town", id="town missing"),
    pytest.param({"town": "T" * 36}, IssueCode.FIELD_TOO_LONG, "address.town", id="town 36 chars"),
    pytest.param(
        {"country": "ch"}, IssueCode.INVALID_COUNTRY, "address.country", id="lowercase country"
    ),
    pytest.param(
        {"country": "CHE"}, IssueCode.INVALID_COUNTRY, "address.country", id="alpha-3 country"
    ),
    pytest.param(
        {"country": "XX"},
        IssueCode.INVALID_COUNTRY,
        "address.country",
        id="well-formed but unassigned country",
    ),
]


def _with_changes(name: str, address_changes: dict[str, str]) -> tuple[str, dict[str, str]]:
    changes = dict(address_changes)
    return changes.pop("name", name), changes


@pytest.mark.parametrize(("changes", "expected_code", "field"), PARTY_CASES)
def test_creditor_rules(changes: dict[str, str], expected_code: IssueCode, field: str) -> None:
    name, address_changes = _with_changes("Max Muster & Söhne", changes)
    invoice = make_invoice(
        creditor_name=name, creditor_address=replace(CREDITOR_ADDRESS, **address_changes)
    )

    assert issues_of(invoice) == {(expected_code, f"creditor.{field}", ERROR)}


@pytest.mark.parametrize(("changes", "expected_code", "field"), PARTY_CASES)
def test_debtor_rules(changes: dict[str, str], expected_code: IssueCode, field: str) -> None:
    name, address_changes = _with_changes(DEBTOR.name, changes)
    debtor = Debtor(name=name, address=replace(DEBTOR.address, **address_changes))

    assert issues_of(make_invoice(debtor=debtor)) == {(expected_code, f"debtor.{field}", ERROR)}


@pytest.mark.parametrize("country", ["CH", "LI", "DE", "AQ", "XK"])
def test_known_countries_are_accepted(country: str) -> None:
    debtor = Debtor(name=DEBTOR.name, address=replace(DEBTOR.address, country=country))

    assert validate_invoice(make_invoice(debtor=debtor)) == ()


def test_optional_address_lines_may_be_missing() -> None:
    address = replace(CREDITOR_ADDRESS, street=None, building_number=None)

    assert validate_invoice(make_invoice(creditor_address=address)) == ()


def test_unstructured_message_longer_than_140_is_an_error() -> None:
    invoice = make_invoice(unstructured_message="M" * 141)

    assert issues_of(invoice) == {(IssueCode.FIELD_TOO_LONG, "unstructured_message", ERROR)}


@pytest.mark.parametrize(
    "billing_information",
    [
        pytest.param("S1/10/10201409", id="missing // prefix"),
        pytest.param("//" + "B" * 139, id="141 chars"),
    ],
)
def test_invalid_billing_information_is_only_a_warning(billing_information: str) -> None:
    invoice = make_invoice(unstructured_message=None, billing_information=billing_information)

    assert (IssueCode.INVALID_BILLING_INFORMATION, "billing_information", WARNING) in issues_of(
        invoice
    )
    assert all(issue.severity is WARNING for issue in validate_invoice(invoice))


def test_message_and_billing_information_over_140_together_is_only_a_warning() -> None:
    invoice = make_invoice(unstructured_message="M" * 100, billing_information="//" + "B" * 48)

    assert issues_of(invoice) == {
        (IssueCode.ADDITIONAL_INFORMATION_TOO_LONG, "billing_information", WARNING)
    }


@pytest.mark.parametrize(
    ("procedures", "field"),
    [
        pytest.param(("A1", "A2", "A3"), "alternative_procedures", id="three procedures"),
        pytest.param(("A" * 101,), "alternative_procedures[0]", id="101 chars"),
    ],
)
def test_invalid_alternative_procedures_are_only_warnings(
    procedures: tuple[str, ...], field: str
) -> None:
    invoice = make_invoice(alternative_procedures=procedures)

    assert issues_of(invoice) == {(IssueCode.INVALID_ALTERNATIVE_PROCEDURE, field, WARNING)}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("Max 中", id="CJK"),
        pytest.param("Max \U0001f600", id="emoji"),
        pytest.param("Max\tMuster", id="tab"),
        pytest.param("Max\nMuster", id="newline"),
    ],
)
def test_characters_outside_the_character_set_are_errors(value: str) -> None:
    invoice = make_invoice(creditor_name=value)

    assert issues_of(invoice) == {(IssueCode.INVALID_CHARACTERS, "creditor.name", ERROR)}


def test_characters_outside_the_character_set_in_status_a_lines_are_warnings() -> None:
    invoice = make_invoice(alternative_procedures=("AV1 中",))

    assert issues_of(invoice) == {
        (IssueCode.INVALID_CHARACTERS, "alternative_procedures[0]", WARNING)
    }


@pytest.mark.parametrize("value", [" Max Muster", "Max Muster ", " Max Muster "])
def test_padded_values_are_errors(value: str) -> None:
    invoice = make_invoice(creditor_name=value)

    assert issues_of(invoice) == {(IssueCode.PADDED_VALUE, "creditor.name", ERROR)}


def test_padded_billing_information_is_a_warning() -> None:
    invoice = make_invoice(billing_information="//S1/10/10201409 ")

    assert issues_of(invoice) == {(IssueCode.PADDED_VALUE, "billing_information", WARNING)}
