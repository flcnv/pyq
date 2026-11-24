from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

import pytest

from src.adapters.parsers.qr_bill import QrBillParser
from src.application.errors import NoQrCodeFoundError
from src.application.ports.document_reader import Document
from src.domain.models.invoice import Invoice, InvoiceSource
from src.domain.validation import ensure_valid
from tests.integration.pipeline import document_reader

FIXTURES = Path(__file__).parents[1] / "fixtures"
INVOICE_ID = UUID("00000000-0000-4000-8000-000000000001")
CREATED_AT = datetime(2026, 11, 22, tzinfo=UTC)


async def parse_fixture(name: str) -> Invoice:
    """The real pipeline: document reader → QR parser, as wired in the worker."""
    document = await document_reader().read((FIXTURES / name).read_bytes())
    return await parse_page_image(document)


async def parse_image_fixture(name: str) -> Invoice:
    """The QR decoder alone, on one PNG page image: the form in which the reader passes pages."""
    page = (FIXTURES / name).read_bytes()
    return await parse_page_image(Document(pages=(page,), text=""))


async def parse_page_image(document: Document) -> Invoice:
    return await QrBillParser().parse(document, invoice_id=INVOICE_ID, created_at=CREATED_AT)


def assert_is_annex_a_example_1(invoice: Invoice) -> None:

    assert invoice.source is InvoiceSource.QR
    assert invoice.id == INVOICE_ID
    assert invoice.creditor.iban == "CH6431961000004421557"
    assert invoice.creditor.name == "Max Muster & Söhne"  # UTF-8 umlaut survives decoding
    assert invoice.amount.value == Decimal("50.00")
    assert invoice.reference.value == "000008207791225857421286694"
    assert invoice.unstructured_message == "Bezahlung der Reise"
    assert invoice.warnings == ()


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("invoice_with_qr.pdf", id="PDF"),
        pytest.param("invoice_qr_on_page_2.pdf", id="PDF, QR on page 2"),
        pytest.param("invoice_two_qr_codes.pdf", id="PDF with a website QR code too"),
    ],
)
async def test_decodes_annex_a_example_1_from_a_pdf(name: str) -> None:
    assert_is_annex_a_example_1(ensure_valid(await parse_fixture(name)))


async def test_decodes_annex_a_example_1_from_a_page_image() -> None:
    invoice = await parse_image_fixture("qr_example_1.png")

    assert_is_annex_a_example_1(ensure_valid(invoice))


async def test_decodes_the_densest_possible_code() -> None:
    """997 characters (QR version 26) at its printed size: guards the render DPI."""
    invoice = await parse_fixture("invoice_max_density_qr.pdf")

    assert invoice.unstructured_message == "Ö" * 70 + "x" * 70
    assert invoice.billing_information == "//" + "B" * 138


@pytest.mark.parametrize(
    "name",
    [
        pytest.param("invoice_without_qr.pdf", id="PDF without QR code"),
        pytest.param("invoice_without_qr_with_reference.pdf", id="PDF with a reference, no QR"),
        pytest.param("invoice_without_qr_image_only.pdf", id="scanned PDF, no QR"),
    ],
)
async def test_documents_without_a_swiss_qr_code_raise_no_qr_found(name: str) -> None:
    with pytest.raises(NoQrCodeFoundError):
        await parse_fixture(name)


async def test_page_with_only_a_website_qr_code_raises_no_qr_found() -> None:
    with pytest.raises(NoQrCodeFoundError):
        await parse_image_fixture("qr_not_swiss.png")
