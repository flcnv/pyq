import hashlib
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from src.application.models.ingest_job import IngestJob, JobStatus
from src.application.ports.document_reader import DocumentType
from src.application.ports.document_storage import StoredDocument

T0 = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


def make_document(content: bytes = b"%PDF-1.7 test", **changes: Any) -> StoredDocument:
    document = StoredDocument(
        id=uuid4(),
        content=content,
        content_type=DocumentType.PDF,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        created_at=T0,
    )
    return replace(document, **changes)


def make_job(document: StoredDocument | None = None, **changes: Any) -> IngestJob:
    """A fresh `pending` job for `document` (or for a new document)."""
    document = document or make_document(content=uuid4().bytes)
    job = IngestJob(
        id=uuid4(),
        document_id=document.id,
        content_sha256=document.sha256,
        status=JobStatus.PENDING,
        attempts=0,
        run_after=T0,
        locked_by=None,
        locked_until=None,
        error=None,
        invoice_id=None,
        client_id="203.0.113.7",
        request_id="req-1",
        created_at=T0,
        updated_at=T0,
    )
    return replace(job, **changes)
