from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel

from src.application.ports.invoice_repository import InvoicePage
from src.domain.models.amount import Amount, Currency
from src.domain.models.invoice import Invoice, InvoiceSource
from src.domain.models.party import Address, Creditor, Debtor
from src.domain.models.reference import Reference, ReferenceType
from src.domain.models.validation_issue import IssueCode, IssueSeverity, ValidationIssue


class AddressResponse(BaseModel):
    street: str | None
    building_number: str | None
    postal_code: str
    town: str
    country: str

    @classmethod
    def from_domain(cls, address: Address) -> AddressResponse:
        return cls(
            street=address.street,
            building_number=address.building_number,
            postal_code=address.postal_code,
            town=address.town,
            country=address.country,
        )


class CreditorResponse(BaseModel):
    iban: str
    name: str
    address: AddressResponse

    @classmethod
    def from_domain(cls, creditor: Creditor) -> CreditorResponse:
        return cls(
            iban=creditor.iban,
            name=creditor.name,
            address=AddressResponse.from_domain(creditor.address),
        )


class DebtorResponse(BaseModel):
    name: str
    address: AddressResponse

    @classmethod
    def from_domain(cls, debtor: Debtor) -> DebtorResponse:
        return cls(name=debtor.name, address=AddressResponse.from_domain(debtor.address))


class AmountResponse(BaseModel):
    # Decimal is serialized as a JSON string ("50.00"): exact, never a float.
    value: Decimal | None
    currency: Currency

    @classmethod
    def from_domain(cls, amount: Amount) -> AmountResponse:
        return cls(value=amount.value, currency=amount.currency)


class ReferenceResponse(BaseModel):
    type: ReferenceType
    value: str | None

    @classmethod
    def from_domain(cls, reference: Reference) -> ReferenceResponse:
        return cls(type=reference.type, value=reference.value)


class IssueResponse(BaseModel):
    code: IssueCode
    field: str
    message: str
    severity: IssueSeverity
    related_invoice_ids: list[UUID]

    @classmethod
    def from_domain(cls, issue: ValidationIssue) -> IssueResponse:
        return cls(
            code=issue.code,
            field=issue.field,
            message=issue.message,
            severity=issue.severity,
            related_invoice_ids=list(issue.related_invoice_ids),
        )


class InvoiceResponse(BaseModel):
    id: UUID
    created_at: datetime
    source: InvoiceSource
    is_notification: bool
    creditor: CreditorResponse
    amount: AmountResponse
    debtor: DebtorResponse | None
    reference: ReferenceResponse
    unstructured_message: str | None
    billing_information: str | None
    alternative_procedures: list[str]
    warnings: list[IssueResponse]

    @classmethod
    def from_domain(cls, invoice: Invoice) -> InvoiceResponse:
        return cls(
            id=invoice.id,
            created_at=invoice.created_at,
            source=invoice.source,
            is_notification=invoice.is_notification,
            creditor=CreditorResponse.from_domain(invoice.creditor),
            amount=AmountResponse.from_domain(invoice.amount),
            debtor=DebtorResponse.from_domain(invoice.debtor) if invoice.debtor else None,
            reference=ReferenceResponse.from_domain(invoice.reference),
            unstructured_message=invoice.unstructured_message,
            billing_information=invoice.billing_information,
            alternative_procedures=list(invoice.alternative_procedures),
            warnings=[IssueResponse.from_domain(warning) for warning in invoice.warnings],
        )


class InvoicePageResponse(BaseModel):
    items: list[InvoiceResponse]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_domain(cls, page: InvoicePage, *, limit: int, offset: int) -> InvoicePageResponse:
        return cls(
            items=[InvoiceResponse.from_domain(invoice) for invoice in page.items],
            total=page.total,
            limit=limit,
            offset=offset,
        )
