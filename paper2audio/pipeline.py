"""The whole conversion, shared by the command line and the GUI."""

from pathlib import Path

from . import audio, clean, describe, extract, server, tts
from .progress import Progress, print_progress

SUPPORTED_INPUTS = (".pdf", ".txt", ".md")


def convert(
    input_path: Path,
    output_path: Path,
    voice: str,
    vision_model: str | None,
    progress: Progress = print_progress,
) -> float:
    """Convert a paper to an audio file and return its length in minutes.

    With a vision model, figures in PDFs are described and read along with the text.
    Raises RuntimeError if something goes wrong. Any exception raised by `progress`
    (e.g. to cancel) propagates, and a temporary ollama server is still shut down.
    """
    # The server is needed already while loading, to describe the figures.
    with server.ollama_server(progress):
        text = load_text(input_path, vision_model, progress)
        if not text:
            hint = " (is it a scanned image?)" if input_path.suffix.lower() == ".pdf" else ""
            raise RuntimeError(f"no readable text found in {input_path}{hint}")
        result = tts.synthesize(text, voice, progress)
    audio.save(result, output_path)
    minutes = len(result.pcm) / (2 * result.channels * result.sample_rate) / 60
    progress(f"Wrote {output_path} ({minutes:.1f} min)", 1.0)
    return minutes


def load_text(path: Path, vision_model: str | None, progress: Progress = print_progress) -> str:
    """Read the input file and return cleaned, speakable text."""
    progress(f"Extracting text from {path}...", None)
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        pages = extract.extract_pages(path)
        if vision_model is None:
            return clean.clean_pages(pages)
        progress(f"Extracting images from {path}...", None)
        return clean.clean_pages(
            pages,
            extract.extract_images(path, progress),
            lambda images: describe.describe_images(images, vision_model, progress),
        )
    text = path.read_text(encoding="utf-8", errors="replace")
    return clean.clean_markdown(text) if suffix == ".md" else clean.clean_text(text)
