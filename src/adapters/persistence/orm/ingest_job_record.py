from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.adapters.persistence.orm.base import Base, string_enum
from src.application.models.ingest_job import JobStatus


class IngestJobRecord(Base):
    __tablename__ = "ingest_jobs"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    document_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("documents.id", ondelete="SET NULL")
    )
    # One job per file: this is what makes uploads idempotent.
    content_sha256: Mapped[str] = mapped_column(String(64), unique=True)
    status: Mapped[JobStatus] = mapped_column(string_enum(JobStatus, "status"))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    run_after: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_message: Mapped[str | None] = mapped_column(Text)
    # Any: a list of JSON objects shaped like JobErrorDetail; mapped explicitly in the repository.
    error_details: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    # Deferred: completing a job marks it `done` (the fenced update, which also row-locks it)
    # before inserting the invoice it points to; the reference is checked at commit.
    invoice_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("invoices.id", deferrable=True, initially="DEFERRED")
    )
    client_id: Mapped[str | None] = mapped_column(String(64))
    request_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("(status = 'done') = (invoice_id IS NOT NULL)", name="done_has_invoice"),
        CheckConstraint(
            "status <> 'processing' OR (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name="processing_is_locked",
        ),
        CheckConstraint("attempts >= 0", name="attempts_not_negative"),
        Index("ix_ingest_jobs_status_run_after", "status", "run_after"),  # the claim query
    )
