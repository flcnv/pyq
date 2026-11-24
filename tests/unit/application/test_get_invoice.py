from uuid import uuid4

import pytest

from src.application.errors import InvoiceNotFoundError
from src.application.services.get_invoice import GetInvoice
from tests.unit.application.fakes import InMemoryInvoiceRepository, InMemoryUnitOfWork
from tests.unit.domain.factories import make_invoice


async def test_returns_the_stored_invoice() -> None:
    invoice = make_invoice()
    service = GetInvoice(
        unit_of_work=InMemoryUnitOfWork(invoices=InMemoryInvoiceRepository((invoice,))).factory
    )

    assert await service.execute(invoice.id) == invoice


async def test_unknown_id_raises_not_found() -> None:
    service = GetInvoice(
        unit_of_work=InMemoryUnitOfWork(invoices=InMemoryInvoiceRepository()).factory
    )
    missing_id = uuid4()

    with pytest.raises(InvoiceNotFoundError) as raised:
        await service.execute(missing_id)

    assert raised.value.invoice_id == missing_id
