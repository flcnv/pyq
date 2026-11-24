from dataclasses import dataclass


@dataclass(frozen=True, slots=True, kw_only=True)
class Address:
    """Structured address (AdrTp `S`), shared by creditor and debtor (IG v2.4, ch. 4.2.2)."""

    street: str | None
    building_number: str | None
    postal_code: str
    town: str
    country: str


@dataclass(frozen=True, slots=True, kw_only=True)
class Creditor:
    iban: str
    name: str
    address: Address


@dataclass(frozen=True, slots=True, kw_only=True)
class Debtor:
    name: str
    address: Address
