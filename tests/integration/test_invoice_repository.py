from collections.abc import AsyncIterator, Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.adapters.persistence.database import create_engine, create_session_factory
from src.adapters.persistence.repository import SqlAlchemyInvoiceRepository
from src.application.ports.invoice_repository import InvoiceQuery
from src.domain.models.amount import Currency
from src.domain.models.invoice import Invoice, InvoiceSource
from src.domain.models.reference import ReferenceType
from src.domain.validation import ensure_valid
from tests.unit.domain.factories import CREDITOR_REFERENCE, IBAN, QR_IBAN, make_invoice

SessionFactory = async_sessionmaker[AsyncSession]
START = datetime(2026, 11, 22, 8, 30, 15, 123456, tzinfo=UTC)


@pytest.fixture
async def session(session_factory: SessionFactory) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        yield session


@pytest.fixture
def repository(session: AsyncSession) -> SqlAlchemyInvoiceRepository:
    return SqlAlchemyInvoiceRepository(session)


def full_invoice() -> Invoice:
    """Every optional field filled, including warnings, as ensure_valid returns it."""
    return ensure_valid(
        make_invoice(
            currency=Currency.EUR,  # QR-IBAN + QRR in EUR: deprecation warning
            amount=Decimal("1949.75"),
            billing_information="//S1/10/1234/11/201021/30/102673386/32/7.7/40/0:30",
            alternative_procedures=("eBill/B/simon.muster@example.com", "XY;XYService", "AV3"),
        )
    )


def minimal_invoice() -> Invoice:
    """Annex A Example 3 shape: no amount, no debtor, no reference, no message."""
    return ensure_valid(
        make_invoice(
            iban=IBAN,
            amount=None,
            debtor=None,
            reference_type=ReferenceType.NON,
            reference_value=None,
            unstructured_message=None,
        )
    )


@pytest.mark.parametrize("build", [full_invoice, minimal_invoice], ids=["full", "minimal"])
async def test_round_trip_preserves_every_field(
    repository: SqlAlchemyInvoiceRepository, build: Callable[[], Invoice]
) -> None:
    invoice = build()
    await repository.add(invoice)

    assert await repository.get(invoice.id) == invoice


async def test_round_trip_keeps_exact_types(repository: SqlAlchemyInvoiceRepository) -> None:
    invoice = replace(full_invoice(), created_at=START)
    await repository.add(invoice)

    loaded = await repository.get(invoice.id)

    assert loaded is not None
    assert str(loaded.amount.value) == "1949.75"  # Decimal, not float
    assert loaded.created_at == START
    assert loaded.created_at.utcoffset() == timedelta(0)
    assert loaded.source is InvoiceSource.QR
    assert len(loaded.warnings) == 2  # EUR deprecation + 3 alternative procedures


async def test_get_unknown_id_returns_none(repository: SqlAlchemyInvoiceRepository) -> None:
    assert await repository.get(uuid4()) is None


async def add_created_at(
    repository: SqlAlchemyInvoiceRepository, invoice: Invoice, minutes: int
) -> Invoice:
    stored = replace(invoice, id=uuid4(), created_at=START + timedelta(minutes=minutes))
    await repository.add(stored)
    return stored


async def test_list_is_newest_first_with_total(repository: SqlAlchemyInvoiceRepository) -> None:
    invoices = [await add_created_at(repository, make_invoice(), minute) for minute in range(5)]

    page = await repository.list(InvoiceQuery(limit=2, offset=1))

    assert page.items == (invoices[3], invoices[2])
    assert page.total == 5


async def test_list_pages_are_stable_for_equal_timestamps(
    repository: SqlAlchemyInvoiceRepository,
) -> None:
    same_time = [await add_created_at(repository, make_invoice(), 0) for _ in range(4)]

    first = await repository.list(InvoiceQuery(limit=2, offset=0))
    second = await repository.list(InvoiceQuery(limit=2, offset=2))

    assert {*first.items, *second.items} == set(same_time)


async def test_list_filters_by_creditor_iban_and_currency(
    repository: SqlAlchemyInvoiceRepository,
) -> None:
    chf_qr = await add_created_at(repository, make_invoice(), 0)
    eur_qr = await add_created_at(repository, make_invoice(currency=Currency.EUR), 1)
    chf_iban = await add_created_at(
        repository,
        make_invoice(
            iban=IBAN, reference_type=ReferenceType.SCOR, reference_value=CREDITOR_REFERENCE
        ),
        2,
    )

    by_iban = await repository.list(InvoiceQuery(creditor_iban=QR_IBAN))
    by_currency = await repository.list(InvoiceQuery(currency=Currency.CHF))
    by_both = await repository.list(InvoiceQuery(creditor_iban=QR_IBAN, currency=Currency.CHF))

    assert by_iban.items == (eur_qr, chf_qr)
    assert by_currency.items == (chf_iban, chf_qr)
    assert by_both.items == (chf_qr,)
    assert by_both.total == 1


# --- The schema as a last line of defense --------------------------------------------------------


async def test_database_rejects_amount_out_of_range(
    repository: SqlAlchemyInvoiceRepository,
) -> None:
    unvalidated = make_invoice(amount=Decimal("-1.00"))

    with pytest.raises(IntegrityError, match="ck_invoices_amount_range"):
        await repository.add(unvalidated)


async def test_database_rejects_values_longer_than_the_spec(
    repository: SqlAlchemyInvoiceRepository,
) -> None:
    unvalidated = make_invoice(creditor_name="N" * 71)

    with pytest.raises(DBAPIError, match="too long"):
        await repository.add(unvalidated)


async def test_database_rejects_incomplete_debtor(
    repository: SqlAlchemyInvoiceRepository, session: AsyncSession
) -> None:
    invoice = make_invoice()
    await repository.add(invoice)

    with pytest.raises(IntegrityError, match="ck_invoices_debtor_complete"):
        await session.execute(
            text("UPDATE invoices SET debtor_town = NULL WHERE id = :id"),
            {"id": invoice.id},
        )


async def test_database_errors_never_carry_row_values(database_url: URL) -> None:
    # Exception messages end up in logs (`logger.exception`); they must not contain IBANs or names.
    engine = create_engine(database_url)
    invoice = make_invoice()
    try:
        async with create_session_factory(engine)() as session:
            repository = SqlAlchemyInvoiceRepository(session)
            await repository.add(invoice)
            with pytest.raises(IntegrityError) as raised:
                await repository.add(invoice)  # same primary key
    finally:
        await engine.dispose()

    message = str(raised.value)
    assert "SQL parameters hidden" in message
    assert invoice.creditor.iban not in message
    assert invoice.creditor.name not in message
