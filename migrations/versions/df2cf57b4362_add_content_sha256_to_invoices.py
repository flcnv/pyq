"""add content sha256 to invoices

Revision ID: df2cf57b4362
Revises: 22631cd987eb
Create Date: 2026-11-23 12:43:55.344146

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "df2cf57b4362"
down_revision: str | Sequence[str] | None = "22631cd987eb"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Nullable: rows stored before this migration have no file to hash.
    op.add_column("invoices", sa.Column("content_sha256", sa.String(length=64), nullable=True))
    op.create_unique_constraint(
        op.f("uq_invoices_content_sha256"), "invoices", ["content_sha256"]
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_invoices_content_sha256"), "invoices", type_="unique")
    op.drop_column("invoices", "content_sha256")
