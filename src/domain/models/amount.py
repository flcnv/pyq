from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum


class Currency(StrEnum):
    CHF = "CHF"
    EUR = "EUR"


@dataclass(frozen=True, slots=True, kw_only=True)
class Amount:
    """`value` is None when the debtor chooses the amount: Amt is optional, Ccy is mandatory."""

    value: Decimal | None
    currency: Currency
