from dataclasses import replace
from datetime import UTC, datetime, timedelta

from src.application.ports.invoice_repository import InvoiceQuery
from src.application.services.list_invoices import ListInvoices
from src.domain.models.amount import Currency
from src.domain.models.invoice import Invoice
from src.domain.models.reference import ReferenceType
from tests.unit.application.fakes import InMemoryInvoiceRepository, InMemoryUnitOfWork
from tests.unit.domain.factories import CREDITOR_REFERENCE, IBAN, QR_IBAN, make_invoice

START = datetime(2026, 11, 22, tzinfo=UTC)


def invoices_created_one_hour_apart(count: int) -> list[Invoice]:
    return [
        replace(make_invoice(), created_at=START + timedelta(hours=hour)) for hour in range(count)
    ]


async def test_returns_newest_first_with_total_ignoring_the_page() -> None:
    invoices = invoices_created_one_hour_apart(5)
    service = ListInvoices(
        unit_of_work=InMemoryUnitOfWork(invoices=InMemoryInvoiceRepository(tuple(invoices))).factory
    )

    page = await service.execute(InvoiceQuery(limit=2, offset=1))

    assert page.items == (invoices[3], invoices[2])
    assert page.total == 5


async def test_filters_by_creditor_iban_and_currency() -> None:
    chf_qr = make_invoice()
    eur_qr = make_invoice(currency=Currency.EUR)
    chf_iban = make_invoice(
        iban=IBAN, reference_type=ReferenceType.SCOR, reference_value=CREDITOR_REFERENCE
    )
    service = ListInvoices(
        unit_of_work=InMemoryUnitOfWork(
            invoices=InMemoryInvoiceRepository((chf_qr, eur_qr, chf_iban))
        ).factory
    )

    page = await service.execute(InvoiceQuery(creditor_iban=QR_IBAN, currency=Currency.CHF))

    assert page.items == (chf_qr,)
    assert page.total == 1


async def test_creditor_iban_filter_accepts_the_printed_form() -> None:
    invoice = make_invoice()
    service = ListInvoices(
        unit_of_work=InMemoryUnitOfWork(invoices=InMemoryInvoiceRepository((invoice,))).factory
    )
    printed = " ".join(QR_IBAN[i : i + 4] for i in range(0, len(QR_IBAN), 4)).lower()

    page = await service.execute(InvoiceQuery(creditor_iban=f" {printed} "))

    assert page.items == (invoice,)
