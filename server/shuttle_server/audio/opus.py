"""Minimal ctypes binding to libopus (encode/decode only).

Avoids the opuslib package, whose find_library('opus') misses Homebrew's
/opt/homebrew/lib on Apple Silicon. Install: `brew install opus` (Mac) or libopus0 (Linux).
"""
from __future__ import annotations

import ctypes
import ctypes.util
import os
import sys

OPUS_APPLICATION_VOIP = 2048
OPUS_APPLICATION_AUDIO = 2049
OPUS_SET_BITRATE_REQUEST = 4002
OPUS_SET_COMPLEXITY_REQUEST = 4010
OPUS_OK = 0

_lib = None


def _candidates() -> list[str]:
    names = []
    env = os.environ.get("SHUTTLE_LIBOPUS")
    if env:
        names.append(env)
    found = ctypes.util.find_library("opus")
    if found:
        names.append(found)
    if sys.platform == "darwin":
        names += ["/opt/homebrew/lib/libopus.dylib", "/usr/local/lib/libopus.dylib",
                  "/opt/homebrew/opt/opus/lib/libopus.0.dylib"]
    else:
        names += ["libopus.so.0", "libopus.so"]
    return names


def lib() -> ctypes.CDLL:
    global _lib
    if _lib is not None:
        return _lib
    errors = []
    for name in _candidates():
        try:
            l = ctypes.CDLL(name)
            break
        except OSError as e:
            errors.append(f"{name}: {e}")
    else:
        raise OSError("libopus not found (brew install opus / apt install libopus0). Tried: " + "; ".join(errors))
    l.opus_encoder_create.restype = ctypes.c_void_p
    l.opus_encoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    l.opus_encode.restype = ctypes.c_int32
    l.opus_encode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int32]
    l.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    l.opus_encoder_ctl.restype = ctypes.c_int
    l.opus_decoder_create.restype = ctypes.c_void_p
    l.opus_decoder_create.argtypes = [ctypes.c_int32, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    l.opus_decode.restype = ctypes.c_int
    l.opus_decode.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int32, ctypes.c_char_p, ctypes.c_int,
                              ctypes.c_int]
    l.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
    _lib = l
    return l


class OpusEncoder:
    def __init__(self, sample_rate: int, channels: int = 1, application: int = OPUS_APPLICATION_AUDIO,
                 bitrate: int | None = None):
        self.sample_rate = sample_rate
        self.channels = channels
        err = ctypes.c_int()
        self._st = lib().opus_encoder_create(sample_rate, channels, application, ctypes.byref(err))
        if err.value != OPUS_OK or not self._st:
            raise RuntimeError(f"opus_encoder_create failed: {err.value}")
        if bitrate:
            lib().opus_encoder_ctl(ctypes.c_void_p(self._st), ctypes.c_int(OPUS_SET_BITRATE_REQUEST),
                                   ctypes.c_int32(bitrate))

    def encode(self, pcm: bytes, frame_size: int) -> bytes:
        """pcm: int16 little-endian, exactly frame_size samples per channel."""
        if len(pcm) != frame_size * self.channels * 2:
            raise ValueError("pcm length does not match frame_size")
        out = ctypes.create_string_buffer(4000)
        n = lib().opus_encode(self._st, pcm, frame_size, out, len(out))
        if n < 0:
            raise RuntimeError(f"opus_encode failed: {n}")
        return out.raw[:n]

    def __del__(self):
        if getattr(self, "_st", None):
            lib().opus_encoder_destroy(self._st)
            self._st = None


class OpusDecoder:
    def __init__(self, sample_rate: int, channels: int = 1):
        self.sample_rate = sample_rate
        self.channels = channels
        err = ctypes.c_int()
        self._st = lib().opus_decoder_create(sample_rate, channels, ctypes.byref(err))
        if err.value != OPUS_OK or not self._st:
            raise RuntimeError(f"opus_decoder_create failed: {err.value}")

    def decode(self, packet: bytes, max_frame_size: int = 5760) -> bytes:
        out = ctypes.create_string_buffer(max_frame_size * self.channels * 2)
        n = lib().opus_decode(self._st, packet, len(packet), out, max_frame_size, 0)
        if n < 0:
            raise RuntimeError(f"opus_decode failed: {n}")
        return out.raw[: n * self.channels * 2]

    def __del__(self):
        if getattr(self, "_st", None):
            lib().opus_decoder_destroy(self._st)
            self._st = None


def available() -> bool:
    try:
        lib()
        return True
    except OSError:
        return False
