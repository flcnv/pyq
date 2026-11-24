from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from src.domain.models.amount import Amount
from src.domain.models.party import Creditor, Debtor
from src.domain.models.reference import Reference
from src.domain.models.validation_issue import ValidationIssue

# IG v2.4, ch. 4.4, Table 10.
NOTIFICATION_MESSAGES = frozenset(
    {
        "NICHT ZUR ZAHLUNG VERWENDEN",
        "NE PAS UTILISER POUR LE PAIEMENT",
        "NON UTILIZZARE PER IL PAGAMENTO",
        "DO NOT USE FOR PAYMENT",
        "BETG DUVRAR PER IL PAJAMENT",
    }
)
NOTIFICATION_AMOUNT = Decimal("0.00")


class InvoiceSource(StrEnum):
    QR = "qr"
    LLM = "llm"


@dataclass(frozen=True, slots=True, kw_only=True)
class Invoice:
    id: UUID
    created_at: datetime
    source: InvoiceSource
    creditor: Creditor
    amount: Amount
    debtor: Debtor | None
    reference: Reference
    unstructured_message: str | None
    billing_information: str | None
    alternative_procedures: tuple[str, ...]
    warnings: tuple[ValidationIssue, ...] = ()

    @property
    def is_notification(self) -> bool:
        """A "DO NOT USE FOR PAYMENT" bill: amount 0.00 plus one of the fixed messages (ch. 4.4)."""
        return (
            self.amount.value == NOTIFICATION_AMOUNT
            and self.unstructured_message in NOTIFICATION_MESSAGES
        )
