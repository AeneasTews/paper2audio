"""Command line interface: paper2audio convert --input=paper.pdf --output=paper.mp3"""

import argparse
import signal
import sys
from pathlib import Path

from . import audio, describe, pipeline, tts
from .pipeline import SUPPORTED_INPUTS


def main() -> None:
    parser = argparse.ArgumentParser(prog="paper2audio")
    commands = parser.add_subparsers(dest="command", required=True)
    convert = commands.add_parser("convert", help="convert a paper (PDF, text or Markdown) into an audio file")
    convert.add_argument("--input", required=True, type=Path, help="file to read (.pdf, .txt or .md)")
    convert.add_argument("--output", required=True, type=Path, help="audio file to write (.wav or .mp3)")
    convert.add_argument("--voice", default=tts.DEFAULT_VOICE, help=f"phonon voice (default: {tts.DEFAULT_VOICE})")
    convert.add_argument(
        "--vision-model",
        default=describe.DEFAULT_MODEL,
        help=f"ollama vision model that describes figures in PDFs (default: {describe.DEFAULT_MODEL})",
    )
    convert.add_argument("--no-images", action="store_true", help="don't describe figures, read only the text")
    args = parser.parse_args()

    if args.input.suffix.lower() not in SUPPORTED_INPUTS:
        parser.error(f"--input must end in one of {', '.join(SUPPORTED_INPUTS)}")
    if args.output.suffix.lower() not in audio.SUPPORTED_FORMATS:
        parser.error(f"--output must end in one of {', '.join(audio.SUPPORTED_FORMATS)}")
    if not args.input.is_file():
        parser.error(f"input file not found: {args.input}")

    # Turn kill / closed-terminal signals into a normal exit, so a temporary ollama
    # server gets shut down instead of being left running.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, lambda signum, frame: sys.exit(128 + signum))
    try:
        pipeline.convert(args.input, args.output, args.voice, None if args.no_images else args.vision_model)
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit("Interrupted")


if __name__ == "__main__":
    main()
