"""Extract the text of a PDF, and render its pages as images for the vision model."""

import io
from pathlib import Path

import pypdfium2 as pdfium
from pypdf import PdfReader


def extract_pages(pdf_path: Path) -> list[str]:
    reader = PdfReader(pdf_path)
    return [page.extract_text() or "" for page in reader.pages]


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
