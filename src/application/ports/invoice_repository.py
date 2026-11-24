from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from src.domain.models.amount import Currency
from src.domain.models.invoice import Invoice


@dataclass(frozen=True, slots=True, kw_only=True)
class InvoiceQuery:
    creditor_iban: str | None = None
    currency: Currency | None = None
    limit: int = 20
    offset: int = 0


@dataclass(frozen=True, slots=True, kw_only=True)
class InvoicePage:
    items: tuple[Invoice, ...]
    total: int


class InvoiceRepository(Protocol):
    async def add(self, invoice: Invoice) -> None: ...

    async def get(self, invoice_id: UUID) -> Invoice | None: ...

    async def find_same_payment(self, invoice: Invoice) -> tuple[Invoice, ...]:
        """Stored invoices with the same creditor IBAN, reference type and value, currency and
        amount (two empty amounts are equal). Policy, such as skipping notifications, is the
        caller's."""
        ...

    async def list(self, query: InvoiceQuery) -> InvoicePage:
        """Newest first. `total` counts all matches, ignoring limit and offset."""
        ...
