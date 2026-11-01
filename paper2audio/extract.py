"""Extract raw text from a PDF, one string per page."""

from pathlib import Path

from pypdf import PdfReader


def extract_pages(pdf_path: Path) -> list[str]:
    reader = PdfReader(pdf_path)
    return [page.extract_text() or "" for page in reader.pages]
