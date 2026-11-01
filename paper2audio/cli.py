"""Command line interface: paper2audio convert --input=paper.pdf --output=paper.mp3"""

import argparse
import signal
import sys
from pathlib import Path

from . import audio, clean, extract, server, tts

SUPPORTED_INPUTS = (".pdf", ".txt", ".md")


def main() -> None:
    parser = argparse.ArgumentParser(prog="paper2audio")
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("convert", help="convert a paper (PDF, text or Markdown) into an audio file")
    convert.add_argument("--input", required=True, type=Path, help="file to read (.pdf, .txt or .md)")
    convert.add_argument("--output", required=True, type=Path, help="audio file to write (.wav or .mp3)")
    convert.add_argument("--voice", default="Zoey", help="phonon voice (default: Zoey)")
    args = parser.parse_args()

    if args.input.suffix.lower() not in SUPPORTED_INPUTS:
        parser.error(f"--input must end in one of {', '.join(SUPPORTED_INPUTS)}")
    if args.output.suffix.lower() not in audio.SUPPORTED_FORMATS:
        parser.error(f"--output must end in one of {', '.join(audio.SUPPORTED_FORMATS)}")
    if not args.input.is_file():
        parser.error(f"input file not found: {args.input}")

    print(f"Extracting text from {args.input}...", file=sys.stderr)
    text = _load_text(args.input)
    if not text:
        hint = " (is it a scanned image?)" if args.input.suffix.lower() == ".pdf" else ""
        sys.exit(f"No readable text found in {args.input}{hint}")

    # Turn kill / closed-terminal signals into a normal exit, so a temporary ollama
    # server gets shut down instead of being left running.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: sys.exit(128 + signum))
    try:
        with server.ollama_server():
            result = tts.synthesize(text, args.voice)
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit("Interrupted")
    audio.save(result, args.output)
    minutes = len(result.pcm) / (2 * result.channels * result.sample_rate) / 60
    print(f"Wrote {args.output} ({minutes:.1f} min)", file=sys.stderr)


def _load_text(path: Path) -> str:
    """Read the input file and return cleaned, speakable text."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return clean.clean_pages(extract.extract_pages(path))
    text = path.read_text(encoding="utf-8", errors="replace")
    return clean.clean_markdown(text) if suffix == ".md" else clean.clean_text(text)


if __name__ == "__main__":
    main()
