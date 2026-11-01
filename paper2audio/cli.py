"""Command line interface: paper2audio convert --input=paper.pdf --output=paper.mp3"""

import argparse
import signal
import sys
from pathlib import Path

from . import audio, config, pipeline, tts


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
        path = pipeline.resolve(args.input)
        pipeline.convert(path, args.output, settings)
    except RuntimeError as e:
        sys.exit(f"Error: {e}")
    except KeyboardInterrupt:
        sys.exit("Interrupted")


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
