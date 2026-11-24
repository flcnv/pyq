from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.orm.document_record import DocumentRecord
from src.application.ports.document_reader import DocumentType
from src.application.ports.document_storage import StoredDocument


class PostgresDocumentStorage:
    """`DocumentStorage` in a `bytea` column, on the unit of work's session."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, document: StoredDocument) -> None:
        self._session.add(
            DocumentRecord(
                id=document.id,
                content=document.content,
                content_type=document.content_type.value,
                size=document.size,
                sha256=document.sha256,
                created_at=document.created_at,
            )
        )
        await self._session.flush()

    async def get(self, document_id: UUID) -> StoredDocument | None:
        record = await self._session.get(DocumentRecord, document_id)
        if record is None:
            return None
        return StoredDocument(
            id=record.id,
            content=record.content,
            content_type=DocumentType(record.content_type),
            size=record.size,
            sha256=record.sha256,
            created_at=record.created_at,
        )
