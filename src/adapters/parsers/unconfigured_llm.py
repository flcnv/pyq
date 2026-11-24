from datetime import datetime
from uuid import UUID

from src.application.errors import LlmFallbackNotConfiguredError
from src.application.ports.document_reader import Document
from src.domain.models.invoice import Invoice


class UnconfiguredLlmParser:
    """Stands in for the LLM parser when no API key is set: QR invoices still work, and a
    document without a QR code fails with a clear reason."""

    async def parse(self, document: Document, *, invoice_id: UUID, created_at: datetime) -> Invoice:
        raise LlmFallbackNotConfiguredError(
            "No Swiss QR Code found, and the LLM fallback is not configured"
        )
