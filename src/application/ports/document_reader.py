from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class DocumentType(StrEnum):
    PDF = "application/pdf"


@dataclass(frozen=True, slots=True, kw_only=True)
class Document:
    """Page images as encoded PNG bytes, so this layer doesn't depend on numpy or OpenCV."""

    pages: tuple[bytes, ...]
    text: str


class DocumentReader(Protocol):
    async def read(self, content: bytes) -> Document:
        """Reads a PDF. Raises `UnreadableDocumentError` if the content can't be read as one."""
        ...
