"""Extract raw text and embedded images from a PDF, page by page."""

import hashlib
import io
from pathlib import Path

from PIL import Image
from pypdf import PdfReader

from .progress import Progress, print_progress

# Images smaller than this (in pixels, either side) are icons, bullets or rules, not figures.
MIN_IMAGE_SIDE = 100
# Vision models downscale large images anyway; sending smaller ones is much faster.
MAX_IMAGE_SIDE = 1536


def extract_pages(pdf_path: Path) -> list[str]:
    reader = PdfReader(pdf_path)
    return [page.extract_text() or "" for page in reader.pages]


def extract_images(pdf_path: Path, progress: Progress = print_progress) -> list[list[bytes]]:
    """Return the embedded raster images of each page as PNG bytes.

    Tiny images are skipped, and so are repeats of an image already seen on an earlier
    page (logos, watermarks), so each figure is described only once.
    """
    reader = PdfReader(pdf_path)
    seen: set[bytes] = set()
    pages: list[list[bytes]] = []
    for page_number, page in enumerate(reader.pages, 1):
        images: list[bytes] = []
        try:
            page_images = list(page.images)
        except Exception as e:  # malformed resources shouldn't stop the text from being read
            progress(f"  warning: could not read images on page {page_number}: {e}", None)
            page_images = []
        for image_file in page_images:
            try:
                png = _to_png(image_file.image, seen)
            except Exception as e:  # unsupported encodings, broken streams
                progress(f"  warning: skipping image {image_file.name} on page {page_number}: {e}", None)
                continue
            if png is not None:
                images.append(png)
        pages.append(images)
    return pages


def _to_png(image: Image.Image | None, seen: set[bytes]) -> bytes | None:
    """Convert to RGB PNG, or return None for images that should be skipped."""
    if image is None or min(image.size) < MIN_IMAGE_SIDE:
        return None
    digest = hashlib.sha256(image.tobytes()).digest()
    if digest in seen:
        return None
    seen.add(digest)
    image = image.convert("RGB")  # CMYK, palette and alpha images -> something every model accepts
    image.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()
