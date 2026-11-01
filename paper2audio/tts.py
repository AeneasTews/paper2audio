"""Speak text with ollama's phonon model."""

import io
import subprocess
import sys
import wave
from dataclasses import dataclass

MODEL = "phonon"
# phonon handles long inputs, but chunking gives progress output and means one bad
# chunk doesn't throw away all the audio synthesized so far.
MAX_CHUNK_CHARS = 4000


@dataclass
class Audio:
    pcm: bytes  # 16-bit little-endian PCM
    sample_rate: int
    channels: int


def synthesize(text: str, voice: str) -> Audio:
    chunks = _chunk(text)
    pcm = bytearray()
    params = None
    for i, chunk in enumerate(chunks, 1):
        print(f"Speaking chunk {i}/{len(chunks)}...", file=sys.stderr)
        wav_bytes = _run_phonon(chunk, voice)
        if wav_bytes is None:
            continue
        with wave.open(io.BytesIO(wav_bytes)) as w:
            chunk_params = (w.getframerate(), w.getnchannels(), w.getsampwidth())
            if params is None:
                params = chunk_params
            elif chunk_params != params:
                raise RuntimeError(f"phonon returned inconsistent audio formats: {params} vs {chunk_params}")
            pcm += w.readframes(w.getnframes())
    if params is None:
        raise RuntimeError("phonon produced no audio")
    if params[2] != 2:
        raise RuntimeError(f"expected 16-bit audio from phonon, got {params[2] * 8}-bit")
    return Audio(bytes(pcm), sample_rate=params[0], channels=params[1])


def _run_phonon(text: str, voice: str) -> bytes | None:
    result = subprocess.run(
        ["ollama", "run", MODEL, "--voice", voice],
        input=text.encode(),
        capture_output=True,
    )
    if result.returncode != 0:
        error = result.stderr.decode(errors="replace").strip()
        if "no text to speak" in error:
            print(f"  skipped (phonon: {error})", file=sys.stderr)
            return None
        raise RuntimeError(f"phonon failed: {error}")
    return result.stdout


def _chunk(text: str) -> list[str]:
    """Group paragraphs (one per line) into chunks of at most MAX_CHUNK_CHARS."""
    chunks: list[str] = []
    current = ""
    for paragraph in text.splitlines():
        if current and len(current) + len(paragraph) > MAX_CHUNK_CHARS:
            chunks.append(current)
            current = ""
        current = f"{current}\n\n{paragraph}" if current else paragraph
    if current:
        chunks.append(current)
    return chunks
