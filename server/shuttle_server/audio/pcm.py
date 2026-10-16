"""PCM helpers: WAV I/O, resampling (linear, good enough for speech), framing, levels."""
from __future__ import annotations

import array
import io
import math
import wave


def wav_bytes(pcm: bytes, sample_rate: int, channels: int = 1) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def read_wav(data: bytes) -> tuple[bytes, int]:
    """Returns (mono int16 pcm, sample_rate). Stereo is down-mixed; 8/24/32-bit converted."""
    with wave.open(io.BytesIO(data), "rb") as w:
        ch, width, rate = w.getnchannels(), w.getsampwidth(), w.getframerate()
        frames = w.readframes(w.getnframes())
    if width == 2:
        samples = array.array("h", frames)
    elif width == 1:
        samples = array.array("h", ((b - 128) << 8 for b in frames))
    elif width == 3:
        samples = array.array("h", (int.from_bytes(frames[i:i + 3], "little", signed=True) >> 8
                                    for i in range(0, len(frames), 3)))
    elif width == 4:
        src = array.array("i", frames)
        samples = array.array("h", (s >> 16 for s in src))
    else:
        raise ValueError(f"unsupported sample width {width}")
    if ch > 1:
        samples = array.array("h", (sum(samples[i:i + ch]) // ch for i in range(0, len(samples), ch)))
    return samples.tobytes(), rate


def resample(pcm: bytes, src_rate: int, dst_rate: int) -> bytes:
    if src_rate == dst_rate or not pcm:
        return pcm
    src = array.array("h", pcm)
    n_out = int(len(src) * dst_rate / src_rate)
    out = array.array("h", bytes(2 * n_out))
    ratio = src_rate / dst_rate
    last = len(src) - 1
    for i in range(n_out):
        pos = i * ratio
        j = int(pos)
        frac = pos - j
        a = src[j] if j <= last else src[last]
        b = src[j + 1] if j + 1 <= last else a
        out[i] = int(a + (b - a) * frac)
    return out.tobytes()


def frames(pcm: bytes, frame_samples: int) -> list[bytes]:
    """Split into exact frames, zero-padding the last one."""
    size = frame_samples * 2
    result = []
    for i in range(0, len(pcm), size):
        chunk = pcm[i:i + size]
        if len(chunk) < size:
            chunk += b"\x00" * (size - len(chunk))
        result.append(chunk)
    return result


def rms(pcm: bytes) -> float:
    s = array.array("h", pcm)
    if not s:
        return 0.0
    return math.sqrt(sum(x * x for x in s) / len(s))


def duration_ms(pcm: bytes, sample_rate: int) -> int:
    return int(len(pcm) / 2 * 1000 / sample_rate)


def tone(freq: float, ms: int, sample_rate: int, amplitude: float = 0.3) -> bytes:
    n = int(sample_rate * ms / 1000)
    a = array.array("h", (int(amplitude * 32767 * math.sin(2 * math.pi * freq * i / sample_rate)) for i in range(n)))
    return a.tobytes()
