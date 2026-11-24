from typing import Any
from uuid import UUID

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.adapters.persistence.orm.invoice_record import InvoiceRecord
from src.application.ports.invoice_repository import InvoicePage, InvoiceQuery
from src.domain.models.amount import Amount
from src.domain.models.invoice import Invoice
from src.domain.models.party import Address, Creditor, Debtor
from src.domain.models.reference import Reference
from src.domain.models.validation_issue import IssueCode, IssueSeverity, ValidationIssue


class SqlAlchemyInvoiceRepository:
    """`InvoiceRepository` on the unit of work's session: it never commits by itself."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, invoice: Invoice) -> None:
        # A savepoint: a rejected insert must not abort the rest of the transaction.
        async with self._session.begin_nested():
            self._session.add(_to_record(invoice))

    async def get(self, invoice_id: UUID) -> Invoice | None:
        record = await self._session.get(InvoiceRecord, invoice_id)
        return None if record is None else _to_domain(record)

    async def find_same_payment(self, invoice: Invoice) -> tuple[Invoice, ...]:
        statement = (
            select(InvoiceRecord)
            .where(InvoiceRecord.creditor_iban == invoice.creditor.iban)
            .where(InvoiceRecord.reference_type == invoice.reference.type)
            .where(InvoiceRecord.reference_value == invoice.reference.value)
            .where(InvoiceRecord.currency == invoice.amount.currency)
            .where(InvoiceRecord.amount.is_not_distinct_from(invoice.amount.value))
            .order_by(InvoiceRecord.created_at, InvoiceRecord.id)
        )
        records = (await self._session.scalars(statement)).all()
        return tuple(_to_domain(record) for record in records)

    async def list(self, query: InvoiceQuery) -> InvoicePage:
        filtered = _apply_filters(select(InvoiceRecord), query)
        page = (
            # id as tie-breaker: stable pages when several invoices share a timestamp.
            filtered.order_by(InvoiceRecord.created_at.desc(), InvoiceRecord.id.desc())
            .limit(query.limit)
            .offset(query.offset)
        )
        count = _apply_filters(select(func.count()).select_from(InvoiceRecord), query)
        records = (await self._session.scalars(page)).all()
        total = await self._session.scalar(count)
        return InvoicePage(items=tuple(_to_domain(record) for record in records), total=total or 0)


def _apply_filters[*Ts](statement: Select[*Ts], query: InvoiceQuery) -> Select[*Ts]:
    if query.creditor_iban is not None:
        statement = statement.where(InvoiceRecord.creditor_iban == query.creditor_iban)
    if query.currency is not None:
        statement = statement.where(InvoiceRecord.currency == query.currency)
    return statement


# --- Mapping (ORM models ≠ domain models) --------------------------------------------------------


def _to_record(invoice: Invoice) -> InvoiceRecord:
    creditor, debtor = invoice.creditor, invoice.debtor
    return InvoiceRecord(
        id=invoice.id,
        created_at=invoice.created_at,
        source=invoice.source,
        creditor_iban=creditor.iban,
        creditor_name=creditor.name,
        creditor_street=creditor.address.street,
        creditor_building_number=creditor.address.building_number,
        creditor_postal_code=creditor.address.postal_code,
        creditor_town=creditor.address.town,
        creditor_country=creditor.address.country,
        amount=invoice.amount.value,
        currency=invoice.amount.currency,
        debtor_name=debtor.name if debtor else None,
        debtor_street=debtor.address.street if debtor else None,
        debtor_building_number=debtor.address.building_number if debtor else None,
        debtor_postal_code=debtor.address.postal_code if debtor else None,
        debtor_town=debtor.address.town if debtor else None,
        debtor_country=debtor.address.country if debtor else None,
        reference_type=invoice.reference.type,
        reference_value=invoice.reference.value,
        unstructured_message=invoice.unstructured_message,
        billing_information=invoice.billing_information,
        alternative_procedures=list(invoice.alternative_procedures),
        warnings=[_issue_to_json(warning) for warning in invoice.warnings],
    )


def _to_domain(record: InvoiceRecord) -> Invoice:
    return Invoice(
        id=record.id,
        created_at=record.created_at,
        source=record.source,
        creditor=Creditor(
            iban=record.creditor_iban,
            name=record.creditor_name,
            address=Address(
                street=record.creditor_street,
                building_number=record.creditor_building_number,
                postal_code=record.creditor_postal_code,
                town=record.creditor_town,
                country=record.creditor_country,
            ),
        ),
        amount=Amount(value=record.amount, currency=record.currency),
        debtor=_debtor_to_domain(record),
        reference=Reference(type=record.reference_type, value=record.reference_value),
        unstructured_message=record.unstructured_message,
        billing_information=record.billing_information,
        alternative_procedures=tuple(record.alternative_procedures),
        warnings=tuple(_issue_from_json(warning) for warning in record.warnings),
    )


def _debtor_to_domain(record: InvoiceRecord) -> Debtor | None:
    # The debtor_complete check constraint guarantees these are null together.
    if (
        record.debtor_name is None
        or record.debtor_postal_code is None
        or record.debtor_town is None
        or record.debtor_country is None
    ):
        return None
    return Debtor(
        name=record.debtor_name,
        address=Address(
            street=record.debtor_street,
            building_number=record.debtor_building_number,
            postal_code=record.debtor_postal_code,
            town=record.debtor_town,
            country=record.debtor_country,
        ),
    )


def _issue_to_json(issue: ValidationIssue) -> dict[str, Any]:
    return {
        "code": issue.code.value,
        "field": issue.field,
        "message": issue.message,
        "severity": issue.severity.value,
        "related_invoice_ids": [str(invoice_id) for invoice_id in issue.related_invoice_ids],
    }


def _issue_from_json(data: dict[str, Any]) -> ValidationIssue:
    return ValidationIssue(
        code=IssueCode(data["code"]),
        field=data["field"],
        message=data["message"],
        severity=IssueSeverity(data["severity"]),
        # Absent in warnings stored before the field existed.
        related_invoice_ids=tuple(UUID(value) for value in data.get("related_invoice_ids", [])),
    )
