"""Speech recognition. Interface: `await asr.transcribe(pcm16_mono, sample_rate) -> str`."""
from __future__ import annotations

import abc
import base64
import logging
import re
import time

import aiohttp

from ..audio.pcm import wav_bytes
from ..config import AsrConfig, MockAsrConfig, QwenAsrConfig

log = logging.getLogger(__name__)


class ASR(abc.ABC):
    name = "asr"

    @abc.abstractmethod
    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        """Return the recognised text ('' if nothing was said)."""

    async def close(self) -> None:
        pass


class MockASR(ASR):
    """Returns a fixed sentence: lets you test the whole chain without a model."""

    name = "mock"

    def __init__(self, cfg: MockAsrConfig):
        self.cfg = cfg

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        return self.cfg.text if pcm else ""


def clean_qwen_text(text: str) -> str:
    """Qwen3-ASR may prefix the transcript: 'language Chinese<asr_text>你好'."""
    if "<asr_text>" in text:
        text = text.split("<asr_text>", 1)[1]
    text = re.sub(r"</?asr_text>", "", text)
    text = re.sub(r"^\s*language\s+\w+\s*", "", text, flags=re.IGNORECASE)
    return text.strip()


class QwenASR(ASR):
    """Locally deployed Qwen ASR behind an OpenAI-compatible API (e.g. `vllm serve Qwen/Qwen3-ASR-1.7B`).

    api=transcriptions -> POST {base_url}/audio/transcriptions (multipart file)
    api=chat           -> POST {base_url}/chat/completions with an audio_url data: URI
    """

    name = "qwen"

    def __init__(self, cfg: QwenAsrConfig):
        self.cfg = cfg
        self._session: aiohttp.ClientSession | None = None

    def _http(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=self.cfg.timeout_s))
        return self._session

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.cfg.api_key}"} if self.cfg.api_key else {}

    async def transcribe(self, pcm: bytes, sample_rate: int) -> str:
        if not pcm:
            return ""
        wav = wav_bytes(pcm, sample_rate)
        t0 = time.monotonic()
        base = self.cfg.base_url.rstrip("/")
        if self.cfg.api == "transcriptions":
            form = aiohttp.FormData()
            form.add_field("file", wav, filename="speech.wav", content_type="audio/wav")
            form.add_field("model", self.cfg.model)
            form.add_field("response_format", "json")
            if self.cfg.language:
                form.add_field("language", self.cfg.language)
            if self.cfg.prompt:
                form.add_field("prompt", self.cfg.prompt)
            async with self._http().post(f"{base}/audio/transcriptions", data=form, headers=self._headers()) as r:
                body = await r.text()
                if r.status != 200:
                    raise RuntimeError(f"ASR HTTP {r.status}: {body[:300]}")
                data = await r.json(content_type=None)
                text = data.get("text", "")
        else:
            content = [{"type": "audio_url",
                        "audio_url": {"url": "data:audio/wav;base64," + base64.b64encode(wav).decode()}}]
            if self.cfg.prompt:
                content.insert(0, {"type": "text", "text": self.cfg.prompt})
            payload = {"model": self.cfg.model, "messages": [{"role": "user", "content": content}],
                       "temperature": 0}
            async with self._http().post(f"{base}/chat/completions", json=payload, headers=self._headers()) as r:
                body = await r.text()
                if r.status != 200:
                    raise RuntimeError(f"ASR HTTP {r.status}: {body[:300]}")
                data = await r.json(content_type=None)
                text = data["choices"][0]["message"]["content"] or ""
        text = clean_qwen_text(text)
        log.info("ASR (%s/%s) %.0f ms: %r", self.name, self.cfg.api, (time.monotonic() - t0) * 1000, text)
        return text

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def create_asr(cfg: AsrConfig) -> ASR:
    if cfg.provider == "mock":
        return MockASR(cfg.mock)
    return QwenASR(cfg.qwen)
