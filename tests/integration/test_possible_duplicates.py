from dataclasses import replace
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.adapters.persistence.unit_of_work import SqlAlchemyUnitOfWork
from src.application.models.ingest_job import JobStatus
from src.application.ports.document_reader import Document
from src.domain.models.amount import Currency
from src.domain.models.invoice import Invoice
from src.domain.models.reference import ReferenceType
from tests.integration.pipeline import enqueue, job, worker
from tests.unit.application.fakes import FakeDocumentReader, FakeInvoiceParser
from tests.unit.domain.factories import CREDITOR_REFERENCE, IBAN, make_invoice

type SessionFactory = async_sessionmaker[AsyncSession]

QRR_BILL = make_invoice()
SCOR_BILL = make_invoice(
    iban=IBAN, reference_type=ReferenceType.SCOR, reference_value=CREDITOR_REFERENCE
)
NON_BILL = make_invoice(iban=IBAN, reference_type=ReferenceType.NON, reference_value=None)
NOTIFICATION = make_invoice(amount=Decimal("0.00"), unstructured_message="DO NOT USE FOR PAYMENT")


async def ingest(session_factory: SessionFactory, invoice: Invoice, file: bytes) -> Invoice:
    """Upload `file` and let the real worker process it; the QR parser returns `invoice`."""
    queued = await enqueue(session_factory, b"%PDF-" + file)
    await worker(
        session_factory,
        document_reader=FakeDocumentReader(Document(pages=(b"page.png",), text="")),
        qr_parser=FakeInvoiceParser(invoice),
        llm_parser=FakeInvoiceParser(invoice),
    ).run_until_idle()
    done = await job(session_factory, queued.id)
    assert done.status is JobStatus.DONE  # a different file: always stored, never rejected
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored = await uow.invoices.get(done.invoice_id)  # type: ignore[arg-type]
    assert stored is not None
    return stored


def duplicate_ids(invoice: Invoice) -> list[set[str]]:
    return [
        {str(invoice_id) for invoice_id in warning.related_invoice_ids}
        for warning in invoice.warnings
        if warning.code == "possible_duplicate"
    ]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(QRR_BILL, QRR_BILL, id="QRR"),
        pytest.param(
            SCOR_BILL,
            replace(SCOR_BILL, reference=replace(SCOR_BILL.reference, value="rf18539007547034")),
            id="SCOR, different case",
        ),
        pytest.param(make_invoice(amount=None), make_invoice(amount=None), id="both amounts empty"),
    ],
)
async def test_same_bill_in_another_file_is_flagged(
    session_factory: SessionFactory, first: Invoice, second: Invoice
) -> None:
    stored = await ingest(session_factory, first, b"scan 1")

    flagged = await ingest(session_factory, second, b"scan 2")

    assert duplicate_ids(flagged) == [{str(stored.id)}]
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.invoices.get(flagged.id) == flagged  # stored, warning included


async def test_every_earlier_copy_is_named(session_factory: SessionFactory) -> None:
    first = await ingest(session_factory, QRR_BILL, b"scan 1")
    second = await ingest(session_factory, QRR_BILL, b"scan 2")

    third = await ingest(session_factory, QRR_BILL, b"scan 3")

    assert duplicate_ids(third) == [{str(first.id), str(second.id)}]


@pytest.mark.parametrize(
    ("first", "second"),
    [
        pytest.param(NON_BILL, NON_BILL, id="NON reference"),
        pytest.param(QRR_BILL, make_invoice(amount=Decimal("50.01")), id="different amount"),
        pytest.param(QRR_BILL, make_invoice(amount=None), id="amount vs empty amount"),
        pytest.param(
            make_invoice(
                iban=IBAN,
                reference_type=ReferenceType.SCOR,
                reference_value=CREDITOR_REFERENCE,
                currency=Currency.CHF,
            ),
            make_invoice(
                iban=IBAN,
                reference_type=ReferenceType.SCOR,
                reference_value=CREDITOR_REFERENCE,
                currency=Currency.EUR,
            ),
            id="different currency",
        ),
        pytest.param(NOTIFICATION, NOTIFICATION, id="notification"),
    ],
)
async def test_different_or_unidentifiable_bills_are_not_flagged(
    session_factory: SessionFactory, first: Invoice, second: Invoice
) -> None:
    await ingest(session_factory, first, b"scan 1")

    second_stored = await ingest(session_factory, second, b"scan 2")

    assert duplicate_ids(second_stored) == []
