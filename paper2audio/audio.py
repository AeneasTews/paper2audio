"""Write synthesized audio to disk as WAV or MP3."""

import wave
from pathlib import Path

import lameenc

from .tts import Audio

SUPPORTED_FORMATS = (".wav", ".mp3")


def save(audio: Audio, path: Path) -> None:
    suffix = path.suffix.lower()
    if suffix == ".wav":
        with wave.open(str(path), "wb") as w:
            w.setnchannels(audio.channels)
            w.setsampwidth(2)
            w.setframerate(audio.sample_rate)
            w.writeframes(audio.pcm)
    elif suffix == ".mp3":
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(64)  # plenty for speech
        encoder.set_in_sample_rate(audio.sample_rate)
        encoder.set_channels(audio.channels)
        encoder.set_quality(2)
        path.write_bytes(encoder.encode(audio.pcm) + encoder.flush())
    else:
        raise ValueError(f"unsupported output format {suffix!r}, use one of {SUPPORTED_FORMATS}")
