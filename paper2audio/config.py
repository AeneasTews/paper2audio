"""Settings: built-in defaults, overridden by the config file, overridden by the command line.

The config file lives at $XDG_CONFIG_HOME/paper2audio/config.toml (usually
~/.config/paper2audio/config.toml), for example:

    voice = "Zoey"
    speed = 1.5                # playback speed
    llm_model = "gemma4:12b"   # or "none" to always use the rule-based cleanup

    [sections]
    authors = true
    summary = true
"""

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# Name: (included by default, description)
SECTIONS = {
    "title": (True, "the paper's title"),
    "authors": (False, "authors, affiliations and notes like 'corresponding author'"),
    "keywords": (False, "the keywords list"),
    "metadata": (False, "article type, journal, dates, DOI, license"),
    "abstract": (True, "the abstract"),
    "figures": (True, "spoken descriptions of figures and tables"),
    "formulas": (True, "formulas, spoken in words"),
    "footnotes": (True, "footnotes"),
    "citations": (False, "in-text citations like [12] or (Smith et al., 2020)"),
    "acknowledgements": (False, "the acknowledgements"),
    "declarations": (False, "funding, competing interests, author contributions, data availability"),
    "references": (False, "the reference list"),
    "appendix": (False, "everything after the reference list"),
    "summary": (False, "a short generated summary, read before the paper"),
}
# Sections that only the language model can find; the rule-based cleanup ignores them.
LLM_ONLY_SECTIONS = {"figures", "formulas", "footnotes", "appendix", "summary"}


@dataclass
class Settings:
    voice: str = "Zoey"
    speed: float = 1.0
    llm_model: str | None = None  # None: pick automatically; "none": don't use a language model
    sections: set[str] = field(default_factory=lambda: {name for name, (on, _) in SECTIONS.items() if on})


def config_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "paper2audio" / "config.toml"


def load(path: Path | None = None) -> Settings:
    """Defaults, overridden by the config file if there is one."""
    settings = Settings()
    path = path or config_path()
    if not path.is_file():
        return settings
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: {e}") from None
    unknown = set(data) - {"voice", "speed", "llm_model", "sections"}
    if unknown:
        raise ValueError(f"{path}: unknown setting(s): {', '.join(sorted(unknown))}")
    settings.voice = data.get("voice", settings.voice)
    settings.speed = data.get("speed", settings.speed)
    if isinstance(settings.speed, bool) or not isinstance(settings.speed, int | float):
        raise ValueError(f"{path}: speed must be a number, e.g. 1.5")
    settings.llm_model = data.get("llm_model", settings.llm_model)
    for name, enabled in data.get("sections", {}).items():
        _check_section(name, path)
        if not isinstance(enabled, bool):
            raise ValueError(f"{path}: sections.{name} must be true or false")
        (settings.sections.add if enabled else settings.sections.discard)(name)
    return settings


def apply_overrides(settings: Settings, include: list[str], exclude: list[str]) -> None:
    for name in include:
        _check_section(name)
        settings.sections.add(name)
    for name in exclude:
        _check_section(name)
        settings.sections.discard(name)


def _check_section(name: str, path: Path | None = None) -> None:
    if name not in SECTIONS:
        where = f"{path}: " if path else ""
        raise ValueError(f"{where}unknown section {name!r}, choose from: {', '.join(SECTIONS)}")
