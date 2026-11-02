"""Extract the text of a PDF, and render its pages as images for the vision model."""

import io
import logging
from pathlib import Path

import pypdfium2 as pdfium
from pypdf import PdfReader

# pypdf warns about fonts it can't fully parse (e.g. "fontTools is required ..."), which is
# common and doesn't matter for the text we need.
logging.getLogger("pypdf").setLevel(logging.ERROR)


def extract_pages(pdf_path: Path) -> list[str]:
    reader = PdfReader(pdf_path)
    texts = []
    for index, page in enumerate(reader.pages):
        try:
            texts.append(page.extract_text() or "")
        except Exception:
            # pypdf gives up on some pages, e.g. with LimitReachedError on a page with a huge
            # vector figure; pdfium reads those fine, though with less careful spacing.
            texts.append(_pdfium_text(pdf_path, index))
    return texts


def _pdfium_text(pdf_path: Path, page_index: int) -> str:
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        textpage = pdf[page_index].get_textpage()
        text = textpage.get_text_range().replace("\r\n", "\n").replace("\r", "\n")
        return text.replace("\ufffe", "").replace("\x02", "")  # hyphens at line ends: join the word
    finally:
        pdf.close()


def render_page(pdf_path: Path, page_index: int, dpi: int = 150) -> bytes:
    """Render one page as a PNG image."""
    pdf = pdfium.PdfDocument(pdf_path)
    try:
        image = pdf[page_index].render(scale=dpi / 72).to_pil()
    finally:
        pdf.close()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
