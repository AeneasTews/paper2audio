"""Check what paper2audio needs at runtime, mirroring what setup.sh installs."""

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import describe, models, server, tts

SETUP_SCRIPT = Path(__file__).resolve().parent.parent / "setup.sh"
# Where installers put ollama; an app started from Finder or a launcher may not have
# these on its PATH.
EXTRA_PATHS = ("/usr/local/bin", "/opt/homebrew/bin", str(Path.home() / ".local/bin"))


@dataclass
class Requirement:
    key: str  # "ollama", or the model name to pull
    title: str
    purpose: str
    installed: bool
    required: bool  # False: paper2audio works without it, with fewer features
    size: str = ""


def extend_path() -> None:
    """Make sure the usual install locations of ollama are on PATH."""
    parts = os.environ.get("PATH", "").split(os.pathsep)
    missing = [p for p in EXTRA_PATHS if p not in parts and Path(p).is_dir()]
    os.environ["PATH"] = os.pathsep.join(parts + missing)


def check() -> list[Requirement]:
    """Check ollama and the models. Starts a temporary ollama server if needed."""
    has_ollama = shutil.which("ollama") is not None
    has_phonon = has_vision = False
    if has_ollama:
        try:
            with server.ollama_server(lambda message, fraction: None):
                installed = {m["name"].split(":")[0] for m in server.api("/api/tags").get("models", [])}
                has_phonon = tts.MODEL in installed
                has_vision = bool(models.vision_models())
        except (RuntimeError, OSError):
            pass  # an ollama that doesn't start counts as not set up; setup.sh will say why
    return [
        Requirement("ollama", "Ollama", "Runs the speech and vision models locally", has_ollama, True),
        Requirement(tts.MODEL, f"{tts.MODEL} speech model", "Reads the paper aloud", has_phonon, True),
        Requirement(
            describe.DEFAULT_MODEL, f"{describe.DEFAULT_MODEL} vision model",
            "Describes figures in PDFs (optional)", has_vision, False, size="about 3 GB",
        ),
    ]


def start_setup(models_to_install: list[str]) -> subprocess.Popen:
    """Run setup.sh for ollama and the given models, without reinstalling paper2audio itself.

    Output (stdout and stderr together) is readable from the returned process.
    """
    if not SETUP_SCRIPT.is_file():
        raise RuntimeError(f"setup script not found at {SETUP_SCRIPT}; run setup.sh from the project folder")
    return subprocess.Popen(
        ["sh", str(SETUP_SCRIPT), "--no-command", "--models", " ".join(models_to_install)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,  # so cancelling can stop the installer and its children too
    )
