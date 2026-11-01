"""Command line interface: paper2audio convert --input=paper.pdf --output=paper.mp3"""

import argparse
import signal
import sys
from pathlib import Path

from . import audio, clean, config, document, extract, fetch, hardware, llm, readers, server, transcribe, tts

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


def main() -> None:
    parser = argparse.ArgumentParser(prog="paper2audio")
    commands = parser.add_subparsers(dest="command", required=True)
    formats = ", ".join(audio.SUPPORTED_FORMATS)

    convert = commands.add_parser(
        "convert",
        help="convert a paper (PDF, text or Markdown) into an audio file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_sections_help(),
    )
    convert.add_argument("--input", required=True,
                         help="paper to read: a file (.pdf, .html, .xml, .epub, .tex, .txt, .md), a URL, "
                              "a DOI or an arXiv ID like arxiv:1706.03762")
    convert.add_argument("--output", required=True, type=Path, help=f"audio file to write ({formats})")
    convert.add_argument("--speed", type=float, help="playback speed, e.g. 1.5 (default: 1)")
    convert.add_argument("--voice", help="phonon voice (default: Zoey)")
    convert.add_argument("--llm-model", help="ollama model that reads the pages (default: picked for this machine)")
    convert.add_argument("--no-llm", action="store_true", help="don't use a language model, only the rule-based cleanup")
    convert.add_argument("--include", action="extend", type=_names, default=[], metavar="SECTIONS",
                         help="comma-separated sections to read in addition to the defaults")
    convert.add_argument("--exclude", action="extend", type=_names, default=[], metavar="SECTIONS",
                         help="comma-separated sections to leave out")

    speedup = commands.add_parser("speedup", help="change the speed of an existing audio file")
    speedup.add_argument("--input", required=True, type=Path, help="audio file to read (any common format)")
    speedup.add_argument("--speed", required=True, type=float, help="playback speed, e.g. 1.5")
    speedup.add_argument("--output", type=Path,
                         help=f"audio file to write ({formats}; default: next to the input, e.g. talk_1.5x.mp3)")

    args = parser.parse_args()
    if args.command == "convert":
        _convert(args, parser)
    else:
        _speedup(args, parser)


def _convert(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    _check_output(args.output, parser)
    try:
        settings = config.load()
        config.apply_overrides(settings, args.include, args.exclude)
    except ValueError as e:
        parser.error(str(e))
    settings.voice = args.voice or settings.voice
    settings.llm_model = "none" if args.no_llm else args.llm_model or settings.llm_model
    settings.speed = args.speed or settings.speed
    _check_speed(settings.speed, parser)

    # Turn kill / closed-terminal signals into a normal exit, so a temporary ollama
    # server gets shut down instead of being left running.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: sys.exit(128 + signum))
    try:
        path = fetch.resolve(args.input)
    except fetch.FetchError as e:
        sys.exit(f"Error: {e}")
    if path.suffix.lower() not in SUPPORTED_INPUTS:
        parser.error(f"--input must be one of {', '.join(SUPPORTED_INPUTS)}, a URL, a DOI or an arXiv ID")
    try:
        with server.ollama_server():
            text = _prepare_text(path, settings)
            if not text:
                hint = " (is it a scanned image?)" if path.suffix.lower() == ".pdf" else ""
                sys.exit(f"No readable text found in {args.input}{hint}")
            result = tts.synthesize(text, settings.voice)
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit("Interrupted")
    _save(audio.change_speed(result, settings.speed), args.output)


def _speedup(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    if not args.input.is_file():
        parser.error(f"input file not found: {args.input}")
    _check_speed(args.speed, parser)
    output = args.output or args.input.with_name(f"{args.input.stem}_{args.speed:g}x{args.input.suffix}")
    _check_output(output, parser)
    try:
        original = audio.load(args.input)
    except ValueError as e:
        sys.exit(f"Error: {e}")
    _save(audio.change_speed(original, args.speed), output)


def _save(result: tts.Audio, path: Path) -> None:
    audio.save(result, path)
    minutes, seconds = divmod(round(audio.duration_minutes(result) * 60), 60)
    print(f"Wrote {path} ({minutes}:{seconds:02d})", file=sys.stderr)


def _check_output(path: Path, parser: argparse.ArgumentParser) -> None:
    if path.suffix.lower() not in audio.SUPPORTED_FORMATS:
        parser.error(f"output must end in one of {', '.join(audio.SUPPORTED_FORMATS)}")


def _check_speed(speed: float, parser: argparse.ArgumentParser) -> None:
    if not audio.MIN_SPEED <= speed <= audio.MAX_SPEED:
        parser.error(f"speed must be between {audio.MIN_SPEED:g} and {audio.MAX_SPEED:g}")


def _prepare_text(path: Path, settings: config.Settings) -> str:
    """Read the input file and return cleaned, speakable text, one paragraph per line."""
    model = _choose_model(settings.llm_model)
    sections = settings.sections
    suffix = path.suffix.lower()
    if model is None:
        # Structured formats mark figures, formulas, footnotes and appendices themselves.
        needs_model = config.LLM_ONLY_SECTIONS if suffix == ".pdf" else {"summary"}
        wanted = sorted(sections & needs_model)
        if wanted:
            print(f"Note: without a language model these sections can't be handled: {', '.join(wanted)}", file=sys.stderr)
    else:
        print(f"Using language model {model}", file=sys.stderr)
    try:
        if suffix in STRUCTURED_READERS:
            print(f"Reading {path}...", file=sys.stderr)
            text = document.to_speech(STRUCTURED_READERS[suffix](path), sections, model)
        elif suffix == ".pdf":
            if model:
                text = transcribe.transcribe_pdf(path, model, sections)
            else:
                print(f"Extracting text from {path}...", file=sys.stderr)
                text = clean.clean_pages(extract.extract_pages(path), sections)
        else:
            print(f"Reading {path}...", file=sys.stderr)
            raw = path.read_text(encoding="utf-8", errors="replace")
            text = clean.clean_markdown(raw, sections) if suffix == ".md" else clean.clean_text(raw, sections)
        if model and text and "summary" in sections:
            text = f"Summary: {transcribe.summarize(text, model)}\nNow the paper itself.\n{text}"
    finally:
        if model:
            llm.unload(model)  # free the memory for phonon
    return text


def _choose_model(requested: str | None) -> str | None:
    """The language model to use, or None for the rule-based cleanup."""
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
        print("Note: not enough memory for a language model, using the rule-based cleanup", file=sys.stderr)
        return None
    # The recommended model, or a smaller one if only that is installed.
    candidates = hardware.MODELS[hardware.MODELS.index(recommended):]
    model = next((m for m in candidates if m in installed), None)
    if model is None:
        print(f"Note: no language model installed (run 'ollama pull {recommended}' or setup.sh), "
              "using the rule-based cleanup", file=sys.stderr)
    return model


def _names(value: str) -> list[str]:
    return [name.strip() for name in value.split(",") if name.strip()]


def _sections_help() -> str:
    width = max(map(len, config.SECTIONS))
    lines = [f"  {name:<{width}}  [{'on ' if on else 'off'}]  {description}"
             for name, (on, description) in config.SECTIONS.items()]
    return (
        "sections (default in brackets):\n" + "\n".join(lines)
        + f"\n\nDefaults can be changed in {config.config_path()}, e.g.:\n"
        + '  voice = "Zoey"\n  [sections]\n  authors = true\n'
    )


if __name__ == "__main__":
    main()
