from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from src.application.ports.document_reader import DocumentType


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredDocument:
    id: UUID
    content: bytes
    content_type: DocumentType
    size: int
    sha256: str
    created_at: datetime


class DocumentStorage(Protocol):
    """Uploaded files. Postgres today; an object store (S3) could implement the same port."""

    async def add(self, document: StoredDocument) -> None: ...

    async def get(self, document_id: UUID) -> StoredDocument | None: ...
