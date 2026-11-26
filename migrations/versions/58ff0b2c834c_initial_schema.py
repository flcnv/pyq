"""
Revision ID: 58ff0b2c834c
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "58ff0b2c834c"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "invoices",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source",
            sa.Enum("qr", "llm", name="source", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("creditor_iban", sa.String(length=21), nullable=False),
        sa.Column("creditor_name", sa.String(length=70), nullable=False),
        sa.Column("creditor_street", sa.String(length=70), nullable=True),
        sa.Column("creditor_building_number", sa.String(length=16), nullable=True),
        sa.Column("creditor_postal_code", sa.String(length=16), nullable=False),
        sa.Column("creditor_town", sa.String(length=35), nullable=False),
        sa.Column("creditor_country", sa.String(length=2), nullable=False),
        sa.Column("amount", sa.Numeric(precision=11, scale=2), nullable=True),
        sa.Column(
            "currency",
            sa.Enum("CHF", "EUR", name="currency", native_enum=False, create_constraint=True),
            nullable=False,
        ),
        sa.Column("debtor_name", sa.String(length=70), nullable=True),
        sa.Column("debtor_street", sa.String(length=70), nullable=True),
        sa.Column("debtor_building_number", sa.String(length=16), nullable=True),
        sa.Column("debtor_postal_code", sa.String(length=16), nullable=True),
        sa.Column("debtor_town", sa.String(length=35), nullable=True),
        sa.Column("debtor_country", sa.String(length=2), nullable=True),
        sa.Column(
            "reference_type",
            sa.Enum(
                "QRR",
                "SCOR",
                "NON",
                name="reference_type",
                native_enum=False,
                create_constraint=True,
            ),
            nullable=False,
        ),
        sa.Column("reference_value", sa.String(length=27), nullable=True),
        sa.Column("unstructured_message", sa.String(length=140), nullable=True),
        sa.Column("billing_information", sa.Text(), nullable=True),
        sa.Column(
            "alternative_procedures",
            postgresql.ARRAY(sa.Text()),
            server_default="{}",
            nullable=False,
        ),
        sa.Column(
            "warnings", postgresql.JSONB(astext_type=sa.Text()), server_default="[]", nullable=False
        ),
        sa.CheckConstraint(
            "(debtor_name IS NULL) = (debtor_postal_code IS NULL)"
            " AND (debtor_name IS NULL) = (debtor_town IS NULL)"
            " AND (debtor_name IS NULL) = (debtor_country IS NULL)",
            name=op.f("ck_invoices_debtor_complete"),
        ),
        sa.CheckConstraint(
            "amount >= 0 AND amount <= 999999999.99", name=op.f("ck_invoices_amount_range")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_invoices")),
    )
    op.create_index("ix_invoices_created_at", "invoices", ["created_at"], unique=False)
    op.create_index(
        "ix_invoices_creditor_iban_created_at",
        "invoices",
        ["creditor_iban", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_invoices_creditor_iban_reference_value",
        "invoices",
        ["creditor_iban", "reference_value"],
        unique=False,
    )

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


def downgrade() -> None:
    op.drop_index("ix_ingest_jobs_status_run_after", table_name="ingest_jobs")
    op.drop_table("ingest_jobs")
    op.drop_table("documents")
    op.drop_index("ix_invoices_creditor_iban_reference_value", table_name="invoices")
    op.drop_index("ix_invoices_creditor_iban_created_at", table_name="invoices")
    op.drop_index("ix_invoices_created_at", table_name="invoices")
    op.drop_table("invoices")
