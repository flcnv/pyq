from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from src.domain.models.amount import Amount, Currency
from src.domain.models.invoice import Invoice, InvoiceSource
from src.domain.models.party import Address, Creditor, Debtor
from src.domain.models.reference import Reference, ReferenceType

# Values from IG QR-bill v2.4, Annex A (verified check digits).
QR_IBAN = "CH6431961000004421557"  # Example 1, IID 31961
QR_REFERENCE = "000008207791225857421286694"  # Example 1
IBAN = "CH5800791123000889012"  # Examples 4-6, IID 00791
CREDITOR_REFERENCE = "RF18539007547034"  # Examples 5-6

CREDITOR_ADDRESS = Address(
    street="Musterstrasse",
    building_number="123",
    postal_code="8000",
    town="Seldwyla",
    country="CH",
)
DEBTOR = Debtor(
    name="Simon Muster",
    address=Address(
        street="Musterstrasse",
        building_number="1",
        postal_code="8000",
        town="Seldwyla",
        country="CH",
    ),
)


def make_invoice(
    *,
    iban: str = QR_IBAN,
    creditor_name: str = "Max Muster & Söhne",
    creditor_address: Address = CREDITOR_ADDRESS,
    amount: Decimal | None = Decimal("50.00"),
    currency: Currency = Currency.CHF,
    debtor: Debtor | None = DEBTOR,
    reference_type: ReferenceType = ReferenceType.QRR,
    reference_value: str | None = QR_REFERENCE,
    unstructured_message: str | None = "Payment of travel",
    billing_information: str | None = None,
    alternative_procedures: tuple[str, ...] = (),
) -> Invoice:
    """Annex A Example 1 by default: a valid QR-IBAN + QRR bill over 50.00 CHF."""
    return Invoice(
        id=uuid4(),
        created_at=datetime(2026, 11, 22, tzinfo=UTC),
        source=InvoiceSource.QR,
        creditor=Creditor(iban=iban, name=creditor_name, address=creditor_address),
        amount=Amount(value=amount, currency=currency),
        debtor=debtor,
        reference=Reference(type=reference_type, value=reference_value),
        unstructured_message=unstructured_message,
        billing_information=billing_information,
        alternative_procedures=alternative_procedures,
    )
