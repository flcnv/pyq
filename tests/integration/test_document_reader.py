import io
from pathlib import Path

import cv2
import numpy as np
import pypdfium2 as pdfium
import pytest

from src.adapters.documents import pages_to_read
from src.application.errors import UnreadableDocumentError
from src.application.ports.document_reader import Document
from src.config import Settings
from tests.integration.pipeline import document_reader

FIXTURES = Path(__file__).parents[1] / "fixtures"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
DEFAULTS = Settings.model_construct()  # what `document_reader()` uses
MAX_PAGES = DEFAULTS.max_pdf_pages
MAX_PAGE_PIXELS = DEFAULTS.max_pdf_page_pixels


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


async def read(content: bytes) -> Document:
    return await document_reader().read(content)


def blank_pdf(page_count: int, *, width: float = 595, height: float = 842) -> bytes:
    pdf = pdfium.PdfDocument.new()
    for _ in range(page_count):
        pdf.new_page(width, height)
    buffer = io.BytesIO()
    pdf.save(buffer)
    return buffer.getvalue()


def image_size(page: bytes) -> tuple[int, int]:
    image = cv2.imdecode(np.frombuffer(page, np.uint8), cv2.IMREAD_GRAYSCALE)
    height, width = image.shape[:2]
    return width, height


async def test_pdf_gives_page_images_and_text() -> None:
    document = await read(fixture("invoice_with_qr.pdf"))

    assert len(document.pages) == 1
    assert document.pages[0].startswith(PNG_SIGNATURE)
    width, height = image_size(document.pages[0])
    assert (width, height) == pytest.approx((2480, 3508), abs=1)  # A4 at 300 dpi, rounded
    assert "Rechnung Nr. 2026-0042" in document.text
    assert "Max Muster & Söhne" in document.text
    assert "\r" not in document.text


async def test_pdf_pages_are_read_in_order() -> None:
    document = await read(fixture("invoice_qr_on_page_2.pdf"))

    assert len(document.pages) == 2
    assert document.text.index("Rechnung") < document.text.index("Seite 2 von 2")


async def test_pdf_text_keeps_the_reference_line() -> None:
    document = await read(fixture("invoice_without_qr_with_reference.pdf"))

    assert "Referenz: 00 00082 07791 22585 74212 86694" in document.text


async def test_image_only_pdf_gives_the_page_and_no_text() -> None:
    """A scan without OCR: the page is there, the text layer isn't."""
    document = await read(fixture("invoice_without_qr_image_only.pdf"))

    assert len(document.pages) == 1
    assert document.pages[0].startswith(PNG_SIGNATURE)
    assert document.text == ""


def test_long_pdfs_read_the_first_pages_and_always_the_last() -> None:
    assert pages_to_read(3, max_pages=10) == [0, 1, 2]
    assert pages_to_read(10, max_pages=10) == list(range(10))
    assert pages_to_read(25, max_pages=10) == [*range(9), 24]
    assert pages_to_read(25, max_pages=1) == [24]


@pytest.mark.parametrize("page_count", [MAX_PAGES, MAX_PAGES + 1, MAX_PAGES + 5])
async def test_pages_over_the_limit_are_ignored_not_rejected(page_count: int) -> None:
    document = await read(blank_pdf(page_count))

    assert len(document.pages) == MAX_PAGES


async def test_pdf_over_the_page_limit_still_reads_its_last_page() -> None:
    """The payment part is usually on the last page: it's read even when the middle isn't."""
    pdf = pdfium.PdfDocument(fixture("invoice_qr_on_page_2.pdf"))
    for _ in range(MAX_PAGES):
        pdf.new_page(595, 842, index=1)  # blank pages between the text and the payment part
    buffer = io.BytesIO()
    pdf.save(buffer)
    pdf.close()

    document = await read(buffer.getvalue())

    assert len(document.pages) == MAX_PAGES
    assert "Rechnung" in document.text
    assert "Seite 2 von 2" in document.text


async def test_oversized_page_is_rendered_within_the_pixel_cap() -> None:
    """14400 pt is the largest page PDF allows: ~3.6 billion pixels at 300 dpi."""
    document = await read(blank_pdf(1, width=14400, height=14400))

    width, height = image_size(document.pages[0])
    assert width == height
    assert MAX_PAGE_PIXELS * 0.99 < width * height <= (MAX_PAGE_PIXELS**0.5 + 1) ** 2


async def test_page_within_the_pixel_cap_keeps_300_dpi() -> None:
    """A3 (842 x 1191 pt) is ~17.4 million pixels at 300 dpi: under the default cap."""
    document = await read(blank_pdf(1, width=842, height=1191))

    assert image_size(document.pages[0]) == pytest.approx((3508, 4963), abs=1)


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(b"not a pdf at all", id="garbage"),
        pytest.param(fixture("invoice_with_qr.pdf")[:500], id="truncated PDF"),
        pytest.param(fixture("qr_example_1.png"), id="PNG"),
    ],
)
async def test_unreadable_content_raises(content: bytes) -> None:
    with pytest.raises(UnreadableDocumentError):
        await read(content)
