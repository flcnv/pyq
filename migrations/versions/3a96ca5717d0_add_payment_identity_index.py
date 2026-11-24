"""add payment identity index

Revision ID: 3a96ca5717d0
Revises: df2cf57b4362
Create Date: 2026-11-23 13:06:23.366696

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "3a96ca5717d0"
down_revision: str | Sequence[str] | None = "df2cf57b4362"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # SCOR is case-insensitive and is now stored in upper case: bring older rows in line, so the
    # duplicate lookup's plain equality matches them too.
    op.execute(
        "UPDATE invoices SET reference_value = upper(reference_value) "
        "WHERE reference_type = 'SCOR' AND reference_value <> upper(reference_value)"
    )
    op.create_index(
        op.f("ix_invoices_creditor_iban_reference_value"),
        "invoices",
        ["creditor_iban", "reference_value"],
        unique=False,
    )


def downgrade() -> None:
    # The upper-casing is not reverted: the original case isn't kept, and both forms are valid.
    op.drop_index(op.f("ix_invoices_creditor_iban_reference_value"), table_name="invoices")
