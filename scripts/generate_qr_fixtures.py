# Generates the binary QR fixtures in tests/fixtures/ from the Annex A text fixtures.
# segno, pillow and fpdf2 are not project dependencies (pypdfium2 is); run with:
#   uv run --with segno --with pillow --with fpdf2 python scripts/generate_qr_fixtures.py
import io
from datetime import UTC, datetime
from pathlib import Path

import pypdfium2 as pdfium
import segno
from fpdf import FPDF
from PIL import Image, ImageDraw

FIXTURES = Path(__file__).parents[1] / "tests" / "fixtures"
QR_SIZE_MM = 46  # Swiss QR Code size on the payment part (IG v2.4, ch. 5.2 of the style guide)
CROSS_SIZE_MM = 7
A4_MM = (210, 297)
SCAN_DPI = 150  # enough to read the text by eye; keeps the fixture small
POINTS_PER_INCH = 72


def example_payload() -> str:
    # Real QR bills mostly use CRLF; the text fixture is stored with LF.
    return (FIXTURES / "annex_a_example_1.txt").read_bytes().decode("utf-8").replace("\n", "\r\n")


def max_density_payload() -> str:
    """997 characters, the spec maximum: the densest QR code (version 26) a bill can carry.

    Padded with alternative-procedure lines (status A): parses fine, only warnings in validation.
    """
    lines = [*example_payload().split("\r\n")[:29], "Ö" * 70 + "x" * 70, "EPD", "//" + "B" * 138]
    while len("\r\n".join(lines)) < 997:
        lines.append("A" * min(100, 997 - len("\r\n".join(lines)) - 2))
    payload = "\r\n".join(lines)
    assert len(payload) == 997, len(payload)
    return payload


def swiss_qr_image(payload: str, size_px: int) -> Image.Image:
    """QR code (error correction M, UTF-8) with the Swiss cross in the middle, as on real bills."""
    qr = segno.make_qr(payload, error="m", encoding="utf-8", boost_error=False)
    buffer = io.BytesIO()
    qr.save(buffer, kind="png", scale=10, border=0)
    image = Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")
    image = image.resize((size_px, size_px), Image.Resampling.NEAREST)
    cross = round(size_px * CROSS_SIZE_MM / QR_SIZE_MM)
    left = (size_px - cross) // 2
    draw = ImageDraw.Draw(image)
    draw.rectangle((left, left, left + cross, left + cross), fill="white")
    inner = round(cross * 0.83)
    inner_left = (size_px - inner) // 2
    draw.rectangle((inner_left, inner_left, inner_left + inner, inner_left + inner), fill="black")
    bar_long, bar_short = round(inner * 0.6), round(inner * 0.18)
    center = size_px // 2
    draw.rectangle(
        (
            center - bar_long // 2,
            center - bar_short // 2,
            center + bar_long // 2,
            center + bar_short // 2,
        ),
        fill="white",
    )
    draw.rectangle(
        (
            center - bar_short // 2,
            center - bar_long // 2,
            center + bar_short // 2,
            center + bar_long // 2,
        ),
        fill="white",
    )
    return image


def with_quiet_zone(image: Image.Image, margin: int) -> Image.Image:
    framed = Image.new("RGB", (image.width + 2 * margin, image.height + 2 * margin), "white")
    framed.paste(image, (margin, margin))
    return framed


def save_png(image: Image.Image, name: str) -> None:
    image.save(FIXTURES / name, format="PNG", optimize=True)


def new_pdf() -> FPDF:
    pdf = FPDF(format="A4", unit="mm")
    pdf.set_creator("swiss-invoice-api fixtures")
    pdf.set_creation_date(datetime(2026, 11, 22, tzinfo=UTC))  # reproducible output
    return pdf


def image_only_pdf(name: str, source: str) -> None:
    """`source`'s pages as images, with no text layer: a scan without OCR."""
    pdf = new_pdf()
    document = pdfium.PdfDocument(FIXTURES / source)
    try:
        for page in document:
            bitmap = page.render(scale=SCAN_DPI / POINTS_PER_INCH, grayscale=True)
            buffer = io.BytesIO()
            bitmap.to_pil().save(buffer, format="PNG")
            pdf.add_page()
            pdf.image(buffer, x=0, y=0, w=A4_MM[0], h=A4_MM[1])
    finally:
        document.close()
    (FIXTURES / name).write_bytes(bytes(pdf.output()))


def invoice_pdf(
    name: str,
    pages: list[list[str]],
    qr_on_page: int | None,
    *,
    payload: str | None = None,
    extra_qr: Image.Image | None = None,
) -> None:
    pdf = new_pdf()
    for index, lines in enumerate(pages):
        pdf.add_page()
        pdf.set_font("helvetica", size=12)
        for line in lines:
            pdf.cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
        if index == qr_on_page:
            buffer = io.BytesIO()
            size = 1200 if payload else 543  # dense codes need more pixels to stay sharp
            swiss_qr_image(payload or example_payload(), size).save(buffer, format="PNG")
            pdf.image(buffer, x=67, y=209, w=QR_SIZE_MM, h=QR_SIZE_MM)  # payment part position
            if extra_qr is not None:  # e.g. a website QR code elsewhere on the same page
                other = io.BytesIO()
                extra_qr.save(other, format="PNG")
                pdf.image(other, x=10, y=209, w=40, h=40)
    (FIXTURES / name).write_bytes(bytes(pdf.output()))


INVOICE_TEXT = [
    "Max Muster & Söhne",
    "Musterstrasse 123, 8000 Seldwyla",
    "",
    "Rechnung Nr. 2026-0042",
    "Bezahlung der Reise",
    "Betrag: CHF 50.00",
    "Konto / Zahlbar an: CH64 3196 1000 0044 2155 7",
]
# The QR reference of Annex A example 1, which goes with the QR-IBAN above.
INVOICE_WITH_REFERENCE_TEXT = [*INVOICE_TEXT, "Referenz: 00 00082 07791 22585 74212 86694"]


def main() -> None:
    qr = swiss_qr_image(example_payload(), 543)  # 46 mm at 300 dpi
    save_png(with_quiet_zone(qr, 60), "qr_example_1.png")
    url_qr = segno.make_qr("https://example.com/invoice/42", error="m")
    buffer = io.BytesIO()
    url_qr.save(buffer, kind="png", scale=12, border=4)
    (FIXTURES / "qr_not_swiss.png").write_bytes(buffer.getvalue())
    url_image = Image.open(io.BytesIO(buffer.getvalue())).convert("RGB")
    invoice_pdf("invoice_with_qr.pdf", [INVOICE_TEXT], qr_on_page=0)
    invoice_pdf("invoice_without_qr.pdf", [INVOICE_TEXT], qr_on_page=None)
    invoice_pdf(
        "invoice_without_qr_with_reference.pdf", [INVOICE_WITH_REFERENCE_TEXT], qr_on_page=None
    )
    image_only_pdf("invoice_without_qr_image_only.pdf", "invoice_without_qr_with_reference.pdf")
    invoice_pdf(
        "invoice_qr_on_page_2.pdf",
        [INVOICE_TEXT, ["Zahlteil", "Seite 2 von 2"]],
        qr_on_page=1,
    )
    invoice_pdf("invoice_two_qr_codes.pdf", [INVOICE_TEXT], qr_on_page=0, extra_qr=url_image)
    invoice_pdf(
        "invoice_max_density_qr.pdf", [INVOICE_TEXT], qr_on_page=0, payload=max_density_payload()
    )
    for path in sorted(FIXTURES.glob("*")):
        if path.suffix in {".png", ".pdf"}:
            print(f"{path.name:38} {path.stat().st_size:>8} bytes")


if __name__ == "__main__":
    main()
