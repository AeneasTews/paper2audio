"""Command line interface: paper2audio convert --input=paper.pdf --output=paper.mp3"""

import argparse
import signal
import sys
from pathlib import Path

from . import audio, clean, config, extract, hardware, llm, server, transcribe, tts

SUPPORTED_INPUTS = (".pdf", ".txt", ".md")


def main() -> None:
    parser = argparse.ArgumentParser(prog="paper2audio")
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser(
        "convert",
        help="convert a paper (PDF, text or Markdown) into an audio file",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_sections_help(),
    )
    convert.add_argument("--input", required=True, type=Path, help="file to read (.pdf, .txt or .md)")
    convert.add_argument("--output", required=True, type=Path, help="audio file to write (.wav or .mp3)")
    convert.add_argument("--voice", help="phonon voice (default: Zoey)")
    convert.add_argument("--llm-model", help="ollama model that reads the pages (default: picked for this machine)")
    convert.add_argument("--no-llm", action="store_true", help="don't use a language model, only the rule-based cleanup")
    convert.add_argument("--include", action="extend", type=_names, default=[], metavar="SECTIONS",
                         help="comma-separated sections to read in addition to the defaults")
    convert.add_argument("--exclude", action="extend", type=_names, default=[], metavar="SECTIONS",
                         help="comma-separated sections to leave out")
    args = parser.parse_args()

    if args.input.suffix.lower() not in SUPPORTED_INPUTS:
        parser.error(f"--input must end in one of {', '.join(SUPPORTED_INPUTS)}")
    if args.output.suffix.lower() not in audio.SUPPORTED_FORMATS:
        parser.error(f"--output must end in one of {', '.join(audio.SUPPORTED_FORMATS)}")
    if not args.input.is_file():
        parser.error(f"input file not found: {args.input}")
    try:
        settings = config.load()
        config.apply_overrides(settings, args.include, args.exclude)
    except ValueError as e:
        parser.error(str(e))
    settings.voice = args.voice or settings.voice
    settings.llm_model = "none" if args.no_llm else args.llm_model or settings.llm_model

    # Turn kill / closed-terminal signals into a normal exit, so a temporary ollama
    # server gets shut down instead of being left running.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: sys.exit(128 + signum))
    try:
        with server.ollama_server():
            text = _prepare_text(args.input, settings)
            if not text:
                hint = " (is it a scanned image?)" if args.input.suffix.lower() == ".pdf" else ""
                sys.exit(f"No readable text found in {args.input}{hint}")
            result = tts.synthesize(text, settings.voice)
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit("Interrupted")
    audio.save(result, args.output)
    minutes = len(result.pcm) / (2 * result.channels * result.sample_rate) / 60
    print(f"Wrote {args.output} ({minutes:.1f} min)", file=sys.stderr)


def _prepare_text(path: Path, settings: config.Settings) -> str:
    """Read the input file and return cleaned, speakable text, one paragraph per line."""
    model = _choose_model(settings.llm_model)
    sections = settings.sections
    if model is None:
        wanted = sorted(sections & config.LLM_ONLY_SECTIONS - ({"figures", "formulas"} if path.suffix != ".pdf" else set()))
        if wanted:
            print(f"Note: without a language model these sections can't be handled: {', '.join(wanted)}", file=sys.stderr)
    else:
        print(f"Using language model {model}", file=sys.stderr)
    try:
        if path.suffix.lower() == ".pdf":
            if model:
                text = transcribe.transcribe_pdf(path, model, sections)
            else:
                print(f"Extracting text from {path}...", file=sys.stderr)
                text = clean.clean_pages(extract.extract_pages(path), sections)
        else:
            print(f"Reading {path}...", file=sys.stderr)
            raw = path.read_text(encoding="utf-8", errors="replace")
            text = clean.clean_markdown(raw, sections) if path.suffix.lower() == ".md" else clean.clean_text(raw, sections)
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
