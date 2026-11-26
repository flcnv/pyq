from dataclasses import replace

from src.application.ports.invoice_repository import InvoicePage, InvoiceQuery
from src.application.ports.unit_of_work import UnitOfWorkFactory
from src.domain.validation import normalized_iban


class ListInvoices:
    def __init__(self, *, unit_of_work: UnitOfWorkFactory) -> None:
        self._unit_of_work = unit_of_work

    async def execute(self, query: InvoiceQuery) -> InvoicePage:
        if query.creditor_iban is not None:
            # Stored IBANs are in electronic form; a copied printed one would match nothing.
            query = replace(query, creditor_iban=normalized_iban(query.creditor_iban))
        async with self._unit_of_work() as uow:
            return await uow.invoices.list(query)
