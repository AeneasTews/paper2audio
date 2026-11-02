"""Read, write and speed up audio in common formats, with PyAV (which bundles ffmpeg)."""

from fractions import Fraction
from pathlib import Path

import av

from .tts import Audio

# Output format by file extension: (codec, bit rate). Bit rates are plenty for speech.
FORMATS = {
    ".mp3": ("libmp3lame", 64_000),
    ".m4a": ("aac", 64_000),
    ".opus": ("libopus", 32_000),
    ".ogg": ("libopus", 32_000),
    ".flac": ("flac", None),
    ".wav": ("pcm_s16le", None),
    ".aiff": ("pcm_s16be", None),
}
SUPPORTED_FORMATS = tuple(FORMATS)
# ffmpeg's atempo filter changes speed without changing pitch; it handles 0.5x to 100x,
# beyond 4x speech is no longer understandable.
MIN_SPEED, MAX_SPEED = 0.5, 4.0
FRAME_SAMPLES = 4096


def save(audio: Audio, path: Path, tags: dict[str, str] | None = None) -> None:
    """Write the audio; tags (e.g. title, artist, album) are shown by music players."""
    codec, bit_rate = FORMATS[path.suffix.lower()]
    with av.open(str(path), "w") as container:
        container.metadata.update({k: v for k, v in (tags or {}).items() if v})
        stream = container.add_stream(codec, rate=audio.sample_rate, layout=_layout(audio))
        if bit_rate:
            stream.bit_rate = bit_rate
        context = stream.codec_context
        # Convert to the encoder's sample format, in the frame size it needs (e.g. 1024 for AAC).
        resampler = av.AudioResampler(format=context.format.name, layout=_layout(audio),
                                      rate=audio.sample_rate, frame_size=context.frame_size or None)
        for frame in [*_frames(audio), None]:
            for resampled in resampler.resample(frame):
                container.mux(stream.encode(resampled))
        container.mux(stream.encode(None))


def load(path: Path) -> Audio:
    """Decode any audio file ffmpeg can read into 16-bit PCM."""
    try:
        with av.open(str(path)) as container:
            if not container.streams.audio:
                raise ValueError(f"{path} has no audio")
            stream = container.streams.audio[0]
            channels = min(stream.codec_context.layout.nb_channels, 2)
            rate = stream.codec_context.sample_rate
            resampler = av.AudioResampler(format="s16", layout="mono" if channels == 1 else "stereo", rate=rate)
            pcm = bytearray()
            for frame in [*container.decode(stream), None]:
                for resampled in resampler.resample(frame):
                    pcm += _pcm(resampled)
    except av.FFmpegError as e:
        raise ValueError(f"can't read {path} as audio: {e}") from None
    return Audio(bytes(pcm), sample_rate=rate, channels=channels)


def change_speed(audio: Audio, speed: float) -> Audio:
    """Play faster or slower without changing the pitch of the voice."""
    if speed == 1:
        return audio
    graph = av.filter.Graph()
    source = graph.add_abuffer(format="s16", sample_rate=audio.sample_rate, layout=_layout(audio),
                               time_base=Fraction(1, audio.sample_rate))
    tempo = graph.add("atempo", str(speed))
    sink = graph.add("abuffersink")
    source.link_to(tempo)
    tempo.link_to(sink)
    graph.configure()

    pcm = bytearray()
    for frame in [*_frames(audio), None]:  # None flushes the filter
        source.push(frame)
        while True:
            try:
                pcm += _pcm(sink.pull())
            except (av.BlockingIOError, av.EOFError):
                break
    return Audio(bytes(pcm), sample_rate=audio.sample_rate, channels=audio.channels)


def duration_minutes(audio: Audio) -> float:
    return len(audio.pcm) / (2 * audio.channels * audio.sample_rate) / 60


def _frames(audio: Audio):
    bytes_per_sample = 2 * audio.channels
    step = FRAME_SAMPLES * bytes_per_sample
    for start in range(0, len(audio.pcm), step):
        chunk = audio.pcm[start:start + step]
        frame = av.AudioFrame(format="s16", layout=_layout(audio), samples=len(chunk) // bytes_per_sample)
        plane = frame.planes[0]
        plane.update(chunk.ljust(plane.buffer_size, b"\0"))  # the buffer can be padded for alignment
        frame.sample_rate = audio.sample_rate
        frame.pts = start // bytes_per_sample
        frame.time_base = Fraction(1, audio.sample_rate)
        yield frame


def _pcm(frame: av.AudioFrame) -> bytes:
    return bytes(frame.planes[0])[:frame.samples * frame.layout.nb_channels * 2]


def _layout(audio: Audio) -> str:
    return "mono" if audio.channels == 1 else "stereo"
