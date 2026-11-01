"""The whole conversion, shared by the command line and the GUI."""

from pathlib import Path

from . import audio, clean, config, document, extract, fetch, hardware, llm, readers, server, transcribe, tts
from .progress import Progress, print_progress

# Structured formats, read into labeled blocks (see readers.py)
STRUCTURED_READERS = {
    ".html": lambda path: readers.read_html(path.read_text(errors="replace")),
    ".htm": lambda path: readers.read_html(path.read_text(errors="replace")),
    ".xhtml": lambda path: readers.read_html(path.read_text(errors="replace")),
    ".xml": lambda path: readers.read_jats(path.read_text(errors="replace")),
    ".epub": readers.read_epub,
    ".tex": readers.read_tex,
}
SUPPORTED_INPUTS = (".pdf", ".txt", ".md", *STRUCTURED_READERS)


def resolve(source: str, progress: Progress = print_progress) -> Path:
    """A local file for the input: the file itself, or a download of the paper for an
    arXiv ID, DOI or URL. Raises RuntimeError if it can't be found or read."""
    try:
        path = fetch.resolve(source, progress)
    except fetch.FetchError as e:
        raise RuntimeError(str(e)) from None
    if path.suffix.lower() not in SUPPORTED_INPUTS:
        raise RuntimeError(f"the paper must be one of {', '.join(SUPPORTED_INPUTS)}, a URL, a DOI or an arXiv ID")
    return path


def convert(input_path: Path, output_path: Path, settings: config.Settings,
            progress: Progress = print_progress) -> float:
    """Convert a paper to an audio file and return its length in minutes.

    Raises RuntimeError if something goes wrong. Any exception raised by `progress`
    (e.g. to cancel) propagates, and a temporary ollama server is still shut down.
    """
    with server.ollama_server(progress):
        text = prepare_text(input_path, settings, progress)
        if not text:
            hint = " (is it a scanned image?)" if input_path.suffix.lower() == ".pdf" else ""
            raise RuntimeError(f"no readable text found in {input_path}{hint}")
        result = tts.synthesize(text, settings.voice, progress)
    result = audio.change_speed(result, settings.speed)
    audio.save(result, output_path)
    minutes = audio.duration_minutes(result)
    whole_minutes, seconds = divmod(round(minutes * 60), 60)
    try:
        progress(f"Wrote {output_path} ({whole_minutes}:{seconds:02d})", 1.0)
    except Exception:  # e.g. cancelled at the very last moment: the file is written, so it's done
        pass
    return minutes


def prepare_text(path: Path, settings: config.Settings, progress: Progress = print_progress) -> str:
    """Read the input file and return cleaned, speakable text, one paragraph per line."""
    model = choose_model(settings.llm_model, progress)
    sections = settings.sections
    suffix = path.suffix.lower()
    if model is None:
        # Structured formats mark figures, formulas, footnotes and appendices themselves.
        needs_model = config.LLM_ONLY_SECTIONS if suffix == ".pdf" else {"summary"}
        wanted = sorted(sections & needs_model)
        if wanted:
            progress(f"Note: without a language model these sections can't be handled: {', '.join(wanted)}", None)
    else:
        progress(f"Using language model {model}", None)
    try:
        if suffix in STRUCTURED_READERS:
            progress(f"Reading {path}...", None)
            text = document.to_speech(STRUCTURED_READERS[suffix](path), sections, model, progress)
        elif suffix == ".pdf":
            if model:
                text = transcribe.transcribe_pdf(path, model, sections, progress)
            else:
                progress(f"Extracting text from {path}...", None)
                text = clean.clean_pages(extract.extract_pages(path), sections)
        else:
            progress(f"Reading {path}...", None)
            raw = path.read_text(encoding="utf-8", errors="replace")
            text = clean.clean_markdown(raw, sections) if suffix == ".md" else clean.clean_text(raw, sections)
        if model and text and "summary" in sections:
            text = f"Summary: {transcribe.summarize(text, model, progress)}\nNow the paper itself.\n{text}"
    finally:
        if model:
            llm.unload(model)  # free the memory for phonon
    return text


def choose_model(requested: str | None, progress: Progress = print_progress) -> str | None:
    """The language model to use, or None for the rule-based cleanup.

    requested: a model name, "none", or None to pick one for this machine.
    """
    if requested == "none":
        return None
    installed = llm.installed_models()
    if requested:
        if requested not in installed:
            raise RuntimeError(f"model {requested!r} is not installed. Run 'ollama pull {requested}' or use --no-llm")
        if "completion" not in llm.capabilities(requested):
            raise RuntimeError(f"model {requested!r} can't generate text, pick a language model")
        return requested
    recommended = hardware.recommended_model()
    if recommended is None:
        progress("Note: not enough memory for a language model, using the rule-based cleanup", None)
        return None
    # The recommended model, or a smaller one if only that is installed.
    candidates = hardware.MODELS[hardware.MODELS.index(recommended):]
    model = next((m for m in candidates if m in installed), None)
    if model is None:
        progress(f"Note: no language model installed (run 'ollama pull {recommended}' or setup.sh), "
                 "using the rule-based cleanup", None)
    return model
