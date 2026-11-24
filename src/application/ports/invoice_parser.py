from datetime import datetime
from typing import Protocol
from uuid import UUID

from src.application.ports.document_reader import Document
from src.domain.models.invoice import Invoice


class InvoiceParser(Protocol):
    async def parse(self, document: Document, *, invoice_id: UUID, created_at: datetime) -> Invoice:
        """Extract an invoice. The result is not validated yet: the caller runs `ensure_valid`.

        `invoice_id` and `created_at` come from the caller so parsers stay deterministic.
        """
        ...
