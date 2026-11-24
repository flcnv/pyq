from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.adapters.persistence.orm.base import Base, string_enum
from src.domain.models.amount import Currency
from src.domain.models.invoice import InvoiceSource
from src.domain.models.reference import ReferenceType


class InvoiceRecord(Base):
    """Flat table: creditor and debtor are embedded columns, so they can be filtered and indexed.

    Column lengths mirror the spec limits (IG v2.4, ch. 4.2.2) as a last line of defense. Fields
    whose limits only produce warnings (billing information, alternative procedures) are TEXT.
    """

    __tablename__ = "invoices"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[InvoiceSource] = mapped_column(string_enum(InvoiceSource, "source"))

    creditor_iban: Mapped[str] = mapped_column(String(21))
    creditor_name: Mapped[str] = mapped_column(String(70))
    creditor_street: Mapped[str | None] = mapped_column(String(70))
    creditor_building_number: Mapped[str | None] = mapped_column(String(16))
    creditor_postal_code: Mapped[str] = mapped_column(String(16))
    creditor_town: Mapped[str] = mapped_column(String(35))
    creditor_country: Mapped[str] = mapped_column(String(2))

    amount: Mapped[Decimal | None] = mapped_column(Numeric(11, 2))  # up to 999999999.99
    currency: Mapped[Currency] = mapped_column(string_enum(Currency, "currency"))

    debtor_name: Mapped[str | None] = mapped_column(String(70))
    debtor_street: Mapped[str | None] = mapped_column(String(70))
    debtor_building_number: Mapped[str | None] = mapped_column(String(16))
    debtor_postal_code: Mapped[str | None] = mapped_column(String(16))
    debtor_town: Mapped[str | None] = mapped_column(String(35))
    debtor_country: Mapped[str | None] = mapped_column(String(2))

    reference_type: Mapped[ReferenceType] = mapped_column(
        string_enum(ReferenceType, "reference_type")
    )
    reference_value: Mapped[str | None] = mapped_column(String(27))  # QRR 27, SCOR max 25

    unstructured_message: Mapped[str | None] = mapped_column(String(140))
    billing_information: Mapped[str | None] = mapped_column(Text)
    alternative_procedures: Mapped[list[str]] = mapped_column(ARRAY(Text), server_default="{}")
    # Any: JSON documents shaped like ValidationIssue; mapped explicitly in the repository.
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, server_default="[]")

    __table_args__ = (
        CheckConstraint("amount >= 0 AND amount <= 999999999.99", name="amount_range"),
        # Debtor is all-or-nothing: the required debtor columns are null together.
        CheckConstraint(
            "(debtor_name IS NULL) = (debtor_postal_code IS NULL)"
            " AND (debtor_name IS NULL) = (debtor_town IS NULL)"
            " AND (debtor_name IS NULL) = (debtor_country IS NULL)",
            name="debtor_complete",
        ),
        Index("ix_invoices_created_at", "created_at"),  # list: newest first
        Index("ix_invoices_creditor_iban_created_at", "creditor_iban", "created_at"),  # by creditor
        # find_same_payment: the reference nearly identifies a bill within one creditor account.
        Index("ix_invoices_creditor_iban_reference_value", "creditor_iban", "reference_value"),
    )
