"""add documents and ingest jobs

Revision ID: 61de56a1154d
Revises: 3a96ca5717d0
Create Date: 2026-11-23 14:17:45.152210

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "61de56a1154d"
down_revision: str | Sequence[str] | None = "3a96ca5717d0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Every invoice stored with a file hash gets a `done` job, so a re-upload of its file is still
# recognised once uploads are deduplicated by job. Idempotent: safe to run again.
BACKFILL_DONE_JOBS = """
INSERT INTO ingest_jobs
    (id, document_id, content_sha256, status, attempts, run_after, invoice_id,
     created_at, updated_at)
SELECT gen_random_uuid(), NULL, content_sha256, 'done', 1, created_at, id, created_at, created_at
FROM invoices
WHERE content_sha256 IS NOT NULL
ON CONFLICT (content_sha256) DO NOTHING
"""


def upgrade() -> None:
    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("content_type", sa.String(length=32), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
    )
    op.create_table(
        "ingest_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=True),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "pending",
                "processing",
                "done",
                "failed",
                name="status",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column("run_after", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_by", sa.String(length=100), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("error_details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("invoice_id", sa.Uuid(), nullable=True),
        sa.Column("client_id", sa.String(length=64), nullable=True),
        sa.Column("request_id", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(status = 'done') = (invoice_id IS NOT NULL)",
            name=op.f("ck_ingest_jobs_done_has_invoice"),
        ),
        sa.CheckConstraint(
            "status <> 'processing' OR (locked_by IS NOT NULL AND locked_until IS NOT NULL)",
            name=op.f("ck_ingest_jobs_processing_is_locked"),
        ),
        sa.CheckConstraint("attempts >= 0", name=op.f("ck_ingest_jobs_attempts_not_negative")),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_ingest_jobs_document_id_documents"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["invoice_id"],
            ["invoices.id"],
            name=op.f("fk_ingest_jobs_invoice_id_invoices"),
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingest_jobs")),
        sa.UniqueConstraint("content_sha256", name=op.f("uq_ingest_jobs_content_sha256")),
    )
    op.create_index(
        "ix_ingest_jobs_status_run_after", "ingest_jobs", ["status", "run_after"], unique=False
    )
    op.execute(BACKFILL_DONE_JOBS)


def downgrade() -> None:
    op.drop_index("ix_ingest_jobs_status_run_after", table_name="ingest_jobs")
    op.drop_table("ingest_jobs")
    op.drop_table("documents")
