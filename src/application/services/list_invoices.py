from src.application.ports.invoice_repository import InvoicePage, InvoiceQuery
from src.application.ports.unit_of_work import UnitOfWorkFactory


class ListInvoices:
    def __init__(self, *, unit_of_work: UnitOfWorkFactory) -> None:
        self._unit_of_work = unit_of_work

    async def execute(self, query: InvoiceQuery) -> InvoicePage:
        async with self._unit_of_work() as uow:
            return await uow.invoices.list(query)
