from uuid import UUID

from src.application.errors import InvoiceNotFoundError
from src.application.ports.unit_of_work import UnitOfWorkFactory
from src.domain.models.invoice import Invoice


class GetInvoice:
    def __init__(self, *, unit_of_work: UnitOfWorkFactory) -> None:
        self._unit_of_work = unit_of_work

    async def execute(self, invoice_id: UUID) -> Invoice:
        async with self._unit_of_work() as uow:
            invoice = await uow.invoices.get(invoice_id)
        if invoice is None:
            raise InvoiceNotFoundError(invoice_id)
        return invoice
