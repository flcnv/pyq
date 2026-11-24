import asyncio
from collections.abc import Iterator
from datetime import datetime
from uuid import UUID

import cv2
import numpy as np
from cv2.typing import MatLike

from src.application.errors import NoQrCodeFoundError
from src.application.ports.document_reader import Document
from src.domain.models.invoice import Invoice
from src.domain.qr_payload import parse_qr_payload

SWISS_QR_PREFIX = "SPC"  # QRType, line 1 of every Swiss QR Code payload (ch. 4.2.2)


class QrBillParser:
    """`InvoiceParser` that finds the Swiss QR Code on the page images and parses its payload.

    Uses OpenCV's QR decoder rather than zbar: zbar failed on the Swiss cross in the middle of the
    code and re-encodes UTF-8 bytes with a guessed charset (Big5, Shift-JIS, ...), corrupting
    umlauts. OpenCV decoded every test fixture correctly, including the densest code.
    """

    async def parse(self, document: Document, *, invoice_id: UUID, created_at: datetime) -> Invoice:
        payload = await asyncio.to_thread(find_swiss_qr_payload, document.pages)
        if payload is None:
            raise NoQrCodeFoundError("No Swiss QR Code found in the document")
        return parse_qr_payload(payload, invoice_id=invoice_id, created_at=created_at)


def find_swiss_qr_payload(pages: tuple[bytes, ...]) -> str | None:
    """The first QR code starting with "SPC", page by page. Other QR codes are ignored."""
    detector = cv2.QRCodeDetector()
    for page in pages:
        image = cv2.imdecode(np.frombuffer(page, np.uint8), cv2.IMREAD_GRAYSCALE)
        if image is None:
            continue
        for text in _decode_all(detector, image):
            if text.startswith(SWISS_QR_PREFIX):
                return text
    return None


def _decode_all(detector: cv2.QRCodeDetector, image: MatLike) -> Iterator[str]:
    # Multi first: a page may also carry other QR codes (a website link, ...), and the single
    # decoder returns only one of them. The single decoder is a fallback for codes multi misses.
    found, texts, _, _ = detector.detectAndDecodeMulti(image)
    if found:
        yield from (text for text in texts if text)
    text, _, _ = detector.detectAndDecode(image)
    if text:
        yield text
