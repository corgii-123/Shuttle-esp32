"""Text to speech. Interface: `await tts.synthesize(text) -> (pcm16_mono, sample_rate)`."""
from __future__ import annotations

import abc
import asyncio
import logging
import shutil
import tempfile
from pathlib import Path

import aiohttp

from ..audio import pcm as pcmlib
from ..config import CommandTtsConfig, OpenAiTtsConfig, SayTtsConfig, TtsConfig

log = logging.getLogger(__name__)


class TTS(abc.ABC):
    name = "tts"

    @abc.abstractmethod
    async def synthesize(self, text: str) -> tuple[bytes, int]:
        """Return (int16 mono PCM, sample_rate)."""

    async def close(self) -> None:
        pass


class MockTTS(TTS):
    """Beeps instead of speech (works anywhere): one short tone per ~4 characters, max 3 s."""

    name = "mock"

    def __init__(self, sample_rate: int = 24000):
        self.sample_rate = sample_rate

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        n = max(1, min(12, len(text) // 4))
        out = b""
        for i in range(n):
            out += pcmlib.tone(660 if i % 2 else 880, 150, self.sample_rate, 0.2)
            out += b"\x00\x00" * int(self.sample_rate * 0.1)
        return out, self.sample_rate


class SayTTS(TTS):
    """macOS `say` (built in). Voice 'Tingting' speaks Mandarin."""

    name = "say"

    def __init__(self, cfg: SayTtsConfig, sample_rate: int):
        self.cfg = cfg
        self.sample_rate = sample_rate
        if not shutil.which("say"):
            log.warning("`say` not found (not macOS?) - set tts.provider: mock")

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "tts.wav"
            argv = ["say", "-o", str(out), f"--data-format=LEI16@{self.sample_rate}", "--file-format=WAVE"]
            if self.cfg.voice:
                argv += ["-v", self.cfg.voice]
            if self.cfg.rate:
                argv += ["-r", str(self.cfg.rate)]
            argv += ["--", text]
            proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), 60)
            if proc.returncode != 0:
                raise RuntimeError(f"say failed: {err.decode(errors='replace')[:200]}")
            return pcmlib.read_wav(out.read_bytes())


class OpenAiTTS(TTS):
    """Any OpenAI-compatible /audio/speech endpoint returning WAV."""

    name = "openai"

    def __init__(self, cfg: OpenAiTtsConfig):
        self.cfg = cfg

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        payload = {"model": self.cfg.model, "voice": self.cfg.voice, "input": text, "response_format": "wav"}
        headers = {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.cfg.timeout_s)) as s:
            async with s.post(self.cfg.base_url.rstrip("/") + "/audio/speech", json=payload, headers=headers) as r:
                data = await r.read()
                if r.status != 200:
                    raise RuntimeError(f"TTS HTTP {r.status}: {data[:200]!r}")
        return pcmlib.read_wav(data)


class CommandTTS(TTS):
    """Runs a command that writes a WAV file: argv may use {text} and {out}."""

    name = "command"

    def __init__(self, cfg: CommandTtsConfig):
        self.cfg = cfg

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "tts.wav"
            argv = [a.replace("{text}", text).replace("{out}", str(out)) for a in self.cfg.argv]
            proc = await asyncio.create_subprocess_exec(*argv, stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.PIPE)
            _, err = await asyncio.wait_for(proc.communicate(), 120)
            if proc.returncode != 0 or not out.exists():
                raise RuntimeError(f"TTS command failed: {err.decode(errors='replace')[:200]}")
            return pcmlib.read_wav(out.read_bytes())


class PitchShiftTTS(TTS):
    """Another TTS one pitch higher/lower (tts.pitch_semitones), via ffmpeg: asetrate + atempo keep
    the length and move the formants along, which is what makes an adult voice sound like a child."""

    def __init__(self, inner: TTS, semitones: float):
        self.inner = inner
        self.factor = 2 ** (semitones / 12)
        self.name = f"{inner.name}{semitones:+g}st"
        if not shutil.which("ffmpeg"):
            log.warning("tts.pitch_semitones needs ffmpeg (brew install ffmpeg): voice not shifted")

    async def synthesize(self, text: str) -> tuple[bytes, int]:
        pcm, rate = await self.inner.synthesize(text)
        if not pcm or not shutil.which("ffmpeg"):
            return pcm, rate
        fmt = ["-f", "s16le", "-ar", str(rate), "-ac", "1"]
        argv = ["ffmpeg", "-hide_banner", "-loglevel", "error", *fmt, "-i", "pipe:0",
                "-af", f"asetrate={round(rate * self.factor)},aresample={rate},atempo={1 / self.factor:.5f}",
                *fmt, "pipe:1"]
        proc = await asyncio.create_subprocess_exec(*argv, stdin=asyncio.subprocess.PIPE,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(pcm), 30)
        except asyncio.TimeoutError:
            proc.kill()
            log.warning("pitch shift timed out: original voice")
            return pcm, rate
        if proc.returncode != 0 or not out:
            log.warning("pitch shift failed: %s", err.decode(errors="replace")[:200])
            return pcm, rate
        return out, rate


def create_tts(cfg: TtsConfig, sample_rate: int) -> TTS:
    if cfg.provider == "mock":
        tts: TTS = MockTTS(sample_rate)
    elif cfg.provider == "openai":
        tts = OpenAiTTS(cfg.openai)
    elif cfg.provider == "command":
        tts = CommandTTS(cfg.command)
    else:
        tts = SayTTS(cfg.say, sample_rate)
    return PitchShiftTTS(tts, cfg.pitch_semitones) if cfg.pitch_semitones else tts
