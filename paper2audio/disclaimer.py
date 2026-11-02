"""A short spoken disclaimer, then a chime, at the start of every audio file.

The disclaimer is spoken once and stored: the default voice's recording ships with
paper2audio, and other voices are generated on first use and cached. The file name
includes a hash of the text, so changing the text makes a new recording.
"""

import array
import hashlib
import math
import os
import wave
from pathlib import Path

from . import tts
from .progress import Progress, print_progress
from .tts import Audio

TEXT = (
    "This audio was created automatically by machine learning models and can contain mistakes, "
    "for example in formulas or figure descriptions. Please check the original paper before relying on it."
)
BUNDLED_DIR = Path(__file__).parent / "assets"
PAUSE_SECONDS = 0.5  # before and after the chime
CHIME_TONES = (660.0, 880.0)  # Hz: a rising two-note chime
CHIME_TONE_SECONDS = 0.35
CHIME_VOLUME = 0.25  # of full scale


def prepend(paper: Audio, voice: str, progress: Progress = print_progress) -> Audio:
    """The disclaimer, a pause, a chime, a pause, then the paper."""
    spoken = _recording(voice, progress)
    if (spoken.sample_rate, spoken.channels) != (paper.sample_rate, paper.channels):
        spoken = _record(voice)  # phonon changed its format: record again in the same one
    rate, channels = paper.sample_rate, paper.channels
    pause = _silence(PAUSE_SECONDS, rate, channels)
    pcm = spoken.pcm + pause + _chime(rate, channels) + pause + paper.pcm
    return Audio(pcm, sample_rate=rate, channels=channels)


def file_name(voice: str) -> str:
    digest = hashlib.sha256(TEXT.encode()).hexdigest()[:8]
    return f"disclaimer-{voice}-{digest}.wav"


def _recording(voice: str, progress: Progress) -> Audio:
    bundled = BUNDLED_DIR / file_name(voice)
    if bundled.is_file():
        return _load(bundled)
    cached = _cache_dir() / file_name(voice)
    if cached.is_file():
        return _load(cached)
    progress(f"Recording the disclaimer for voice {voice} (once)...", None)
    audio = _record(voice)
    save(audio, cached)
    return audio


def _record(voice: str) -> Audio:
    return tts.synthesize(TEXT, voice, progress=lambda message, fraction: None)


def save(audio: Audio, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(audio.channels)
        w.setsampwidth(2)
        w.setframerate(audio.sample_rate)
        w.writeframes(audio.pcm)


def _load(path: Path) -> Audio:
    with wave.open(str(path)) as w:
        return Audio(w.readframes(w.getnframes()), sample_rate=w.getframerate(), channels=w.getnchannels())


def _silence(seconds: float, rate: int, channels: int) -> bytes:
    return bytes(2 * channels * int(seconds * rate))


def _chime(rate: int, channels: int) -> bytes:
    """Two soft sine tones, each fading out like a bell."""
    samples = array.array("h")
    length = int(CHIME_TONE_SECONDS * rate)
    for frequency in CHIME_TONES:
        for n in range(length):
            t = n / rate
            envelope = min(1.0, n / (0.005 * rate)) * math.exp(-5 * t)  # 5 ms attack, then decay
            value = int(32767 * CHIME_VOLUME * envelope * math.sin(2 * math.pi * frequency * t))
            samples.extend([value] * channels)
    if array.array("h", [1]).tobytes() != b"\x01\x00":  # WAV/PCM data is little-endian
        samples.byteswap()
    return samples.tobytes()


def _cache_dir() -> Path:
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "paper2audio"
