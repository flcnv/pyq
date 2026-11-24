import asyncio
import logging
import math

import cv2
import numpy as np
import pypdfium2 as pdfium
from cv2.typing import MatLike

from src.application.errors import UnreadableDocumentError
from src.application.ports.document_reader import Document

# 300 dpi: the densest Swiss QR Code (version 26, 997 chars) at its printed 46 mm decodes reliably
# at 200 dpi but not at 150; 300 leaves a margin for codes printed slightly smaller.
RENDER_DPI = 300
POINTS_PER_INCH = 72
RENDER_SCALE = RENDER_DPI / POINTS_PER_INCH

type GrayImage = MatLike  # single-channel 8-bit image

logger = logging.getLogger(__name__)


class PdfiumDocumentReader:
    """`DocumentReader` for PDF: pages rendered with pypdfium2, plus the text layer (no OCR).

    Bounds the work per upload: at most `max_pages` pages, each at most about `max_page_pixels`.
    """

    def __init__(self, *, max_pages: int, max_page_pixels: int) -> None:
        self._max_pages = max_pages
        self._max_page_pixels = max_page_pixels

    async def read(self, content: bytes) -> Document:
        # Rendering and decoding are CPU-bound: keep them off the event loop.
        return await asyncio.to_thread(self._read, content)

    def _read(self, content: bytes) -> Document:
        try:
            pdf = pdfium.PdfDocument(content)
        except pdfium.PdfiumError as error:  # not a PDF, damaged, or password-protected
            raise UnreadableDocumentError("File is not a readable PDF") from error
        try:
            if len(pdf) == 0:
                raise UnreadableDocumentError("PDF has no pages")
            pages, texts = [], []
            for index in pages_to_read(len(pdf), max_pages=self._max_pages):
                image, text = self._read_page(pdf[index], index)
                pages.append(_encode_png(image))
                texts.append(text)
        finally:
            pdf.close()
        return Document(pages=tuple(pages), text="\n".join(texts).strip())

    def _read_page(self, page: pdfium.PdfPage, index: int) -> tuple[GrayImage, str]:
        try:
            text = page.get_textpage().get_text_range().replace("\r\n", "\n")
            scale = render_scale(page.get_size(), max_pixels=self._max_page_pixels)
            if scale < RENDER_SCALE:
                logger.info(
                    "pdf page rendered below %d dpi",
                    RENDER_DPI,
                    extra={"page_index": index, "dpi": round(scale * POINTS_PER_INCH)},
                )
            bitmap = page.render(scale=scale, grayscale=True)
            image: GrayImage = np.squeeze(bitmap.to_numpy())
            return image, text
        finally:
            page.close()


def pages_to_read(page_count: int, *, max_pages: int) -> list[int]:
    """The first pages, plus always the last page (where the payment part usually is).

    Pages beyond `max_pages` are ignored, not rejected: a long invoice still gets its payment part
    read.
    """
    if page_count <= max_pages:
        return list(range(page_count))
    return [*range(max_pages - 1), page_count - 1]


def render_scale(page_size: tuple[float, float], *, max_pixels: int) -> float:
    """`RENDER_SCALE`, or less if the page would have more than `max_pixels` at it.

    A PDF can declare any page size: without this, a huge MediaBox would allocate gigabytes. The
    result can exceed `max_pixels` by one row and column (pdfium rounds the size up).
    """
    width, height = page_size
    pixels = width * height * RENDER_SCALE**2
    if pixels <= max_pixels:
        return RENDER_SCALE
    return RENDER_SCALE * math.sqrt(max_pixels / pixels)


def _encode_png(image: GrayImage) -> bytes:
    ok, buffer = cv2.imencode(".png", image)
    if not ok:
        raise UnreadableDocumentError("Page could not be converted to an image")
    return buffer.tobytes()
