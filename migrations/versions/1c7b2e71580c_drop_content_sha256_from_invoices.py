"""drop content sha256 from invoices

Revision ID: 1c7b2e71580c
Revises: 61de56a1154d
Create Date: 2026-11-23 14:41:44.320874

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "1c7b2e71580c"
down_revision: str | Sequence[str] | None = "61de56a1154d"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Same backfill as 61de56a1154d, run again right before the column goes: invoices stored by the
# synchronous upload path after that migration ran must keep their file recognised too.
BACKFILL_DONE_JOBS = """
INSERT INTO ingest_jobs
    (id, document_id, content_sha256, status, attempts, run_after, invoice_id,
     created_at, updated_at)
SELECT gen_random_uuid(), NULL, content_sha256, 'done', 1, created_at, id, created_at, created_at
FROM invoices
WHERE content_sha256 IS NOT NULL
ON CONFLICT (content_sha256) DO NOTHING
"""

# Each file's hash now lives on its job, the one record of "this file was seen".
RESTORE_HASHES = """
UPDATE invoices SET content_sha256 = ingest_jobs.content_sha256
FROM ingest_jobs
WHERE ingest_jobs.invoice_id = invoices.id
"""


def upgrade() -> None:
    op.execute(BACKFILL_DONE_JOBS)
    op.drop_constraint(op.f("uq_invoices_content_sha256"), "invoices", type_="unique")
    op.drop_column("invoices", "content_sha256")


def downgrade() -> None:
    op.add_column("invoices", sa.Column("content_sha256", sa.String(length=64), nullable=True))
    op.execute(RESTORE_HASHES)
    op.create_unique_constraint(
        op.f("uq_invoices_content_sha256"), "invoices", ["content_sha256"]
    )
