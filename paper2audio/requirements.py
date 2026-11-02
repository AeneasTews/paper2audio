"""Check what paper2audio needs at runtime, mirroring what setup.sh installs."""

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config, hardware, llm, ocr, server, tts

SETUP_SCRIPT = Path(__file__).resolve().parent.parent / "setup.sh"
# Where installers put ollama; an app started from Finder or a launcher may not have
# these on its PATH.
EXTRA_PATHS = ("/usr/local/bin", "/opt/homebrew/bin", str(Path.home() / ".local/bin"))
# Download sizes, as listed by setup.sh.
DOWNLOAD_SIZES = {
    "gemma4:26b": "16 GB",
    "gemma4:12b": "7.7 GB",
    "gemma4:e4b": "9.5 GB" if sys.platform == "darwin" else "6.6 GB",
    "gemma3:4b": "3.4 GB",
    ocr.MODEL: "2.7 GB",
}


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


def wanted_language_model() -> tuple[str | None, list[str]]:
    """The language model to offer installing, and the models that would count as installed.

    Follows the config file: a configured model, "none" (no model wanted), or by default
    the recommendation for this machine, where a smaller recommended model also counts.
    Returns (None, []) if no language model is wanted or the machine is too small.
    """
    try:
        configured = config.load().llm_model
    except ValueError:
        configured = None
    if configured == "none":
        return None, []
    if configured:
        return configured, [configured]
    recommended = hardware.recommended_model()
    if recommended is None:
        return None, []
    return recommended, hardware.MODELS[hardware.MODELS.index(recommended):]


def check() -> list[Requirement]:
    """Check ollama and the models. Starts a temporary ollama server if needed."""
    has_ollama = shutil.which("ollama") is not None
    language_model, acceptable = wanted_language_model()
    has_phonon = has_language_model = has_ocr = False
    if has_ollama:
        try:
            with server.ollama_server(lambda message, fraction: None):
                installed = llm.installed_models()
                has_phonon = tts.MODEL in installed
                has_language_model = any(m in installed for m in acceptable)
                has_ocr = ocr.MODEL in installed
        except (RuntimeError, OSError):
            pass  # an ollama that doesn't start counts as not set up; setup.sh will say why
    requirements = [
        Requirement("ollama", "Ollama", "Runs the speech and language models locally", has_ollama, True),
        Requirement(tts.MODEL, f"{tts.MODEL} speech model", "Reads the paper aloud", has_phonon, True),
    ]
    if language_model:
        requirements.append(Requirement(
            language_model, f"{language_model} language model",
            "Reads the pages: formulas, figures and tables (optional)", has_language_model, False,
            size=DOWNLOAD_SIZES.get(language_model, ""),
        ))
        requirements.append(Requirement(
            ocr.MODEL, "GLM-OCR text recognition",
            "Transcribes PDF pages exactly, with the language model (optional)", has_ocr, False,
            size=DOWNLOAD_SIZES[ocr.MODEL],
        ))
    return requirements


def start_setup(language_model: str | None) -> subprocess.Popen:
    """Run setup.sh for ollama, phonon and the language model (None: no language model),
    without reinstalling paper2audio itself.

    Output (stdout and stderr together) is readable from the returned process.
    """
    if not SETUP_SCRIPT.is_file():
        raise RuntimeError(f"setup script not found at {SETUP_SCRIPT}; run setup.sh from the project folder")
    return subprocess.Popen(
        ["sh", str(SETUP_SCRIPT), "--no-command", language_model or "none"],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env={**os.environ, "PAPER2AUDIO_PYTHON": sys.executable},  # helpers run without uv
        start_new_session=True,  # so cancelling can stop the installer and its children too
    )
