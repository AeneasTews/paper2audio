"""Command line interface: paper2audio convert --input=paper.pdf --output=paper.mp3"""

import argparse
import sys
from pathlib import Path

from . import audio, clean, extract, tts


def main() -> None:
    parser = argparse.ArgumentParser(prog="paper2audio")
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("convert", help="convert a PDF paper into an audio file")
    convert.add_argument("--input", required=True, type=Path, help="PDF file to read")
    convert.add_argument("--output", required=True, type=Path, help="audio file to write (.wav or .mp3)")
    convert.add_argument("--voice", default="Zoey", help="phonon voice (default: Zoey)")
    args = parser.parse_args()

    if args.output.suffix.lower() not in audio.SUPPORTED_FORMATS:
        parser.error(f"--output must end in one of {', '.join(audio.SUPPORTED_FORMATS)}")
    if not args.input.is_file():
        parser.error(f"input file not found: {args.input}")

    print(f"Extracting text from {args.input}...", file=sys.stderr)
    text = clean.clean_pages(extract.extract_pages(args.input))
    if not text:
        sys.exit("No readable text found in the PDF (is it a scanned image?)")

    try:
        result = tts.synthesize(text, args.voice)
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    audio.save(result, args.output)
    minutes = len(result.pcm) / (2 * result.channels * result.sample_rate) / 60
    print(f"Wrote {args.output} ({minutes:.1f} min)", file=sys.stderr)


if __name__ == "__main__":
    main()
