"""/ws/ctrl: xiaozhi-compatible voice/control session (one per device connection).

Device -> server: hello, listen(start/stop/detect), abort, mcp, binary Opus (16 kHz, 60 ms).
Server -> device: hello, stt, llm(emotion), tts(start/sentence_start/stop), binary Opus,
                  plus Shuttle extension {"type":"shuttle","event":"status"|"phone"|"ping",...}.
See docs/protocol.md.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import struct
import time
import uuid
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from ..adb.phone import PhoneUnavailable
from ..audio import pcm as pcmlib
from ..audio.opus import OpusDecoder, OpusEncoder
from .agent import AgentContext

if TYPE_CHECKING:
    from ..app import ShuttleApp

log = logging.getLogger(__name__)


class CtrlSession:
    def __init__(self, ws: web.WebSocketResponse, app: "ShuttleApp", device_id: str, client_id: str,
                 protocol_version: int = 1):
        self.ws = ws
        self.app = app
        self.cfg = app.cfg
        self.device_id = device_id
        self.client_id = client_id
        self.session_id = uuid.uuid4().hex[:12]
        self.version = protocol_version
        self.hello_done = False
        self.listen_mode = "manual"

        v = self.cfg.voice
        self.up_rate = v.uplink_sample_rate
        self.down_rate = v.tts_sample_rate
        self.frame_ms = v.frame_duration_ms
        self._decoder = OpusDecoder(self.up_rate)
        self._listening = False
        self._pcm = bytearray()
        self._voiced = False
        self._silence_ms = 0

        self._task: asyncio.Task | None = None  # utterance -> ASR -> agent -> TTS
        self._tts_task: asyncio.Task | None = None
        self._send_lock = asyncio.Lock()
        self._last_activity = time.monotonic()
        self.created = time.time()
        self.last_text = ""
        self.last_reply = ""

    # ------------------------------------------------------------------ send helpers
    async def send_json(self, obj: dict[str, Any]) -> None:
        if self.ws.closed:
            return
        obj.setdefault("session_id", self.session_id)
        async with self._send_lock:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def send_audio(self, packet: bytes) -> None:
        if self.ws.closed:
            return
        if self.version == 2:
            ts = int(time.monotonic() * 1000) & 0xFFFFFFFF
            packet = struct.pack(">HHIII", 2, 0, 0, ts, len(packet)) + packet
        elif self.version == 3:
            packet = struct.pack(">BBH", 0, 0, len(packet)) + packet
        async with self._send_lock:
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self.ws.send_bytes(packet)

    async def status(self, state: str, text: str = "") -> None:
        """Shuttle extension: drives the e-paper state bar (thinking/acting/done/error/idle)."""
        log.info("[%s] status %s %s", self.device_id, state, text)
        await self.send_json({"type": "shuttle", "event": "status", "state": state, "text": text})

    @property
    def busy(self) -> bool:
        return self._listening or (self._task is not None and not self._task.done())

    async def send_phone_status(self) -> None:
        t = self.app.tunnels.find(self.device_id)
        adb_state = t.adb_state if t else ("device" if self.cfg.adb.fallback_serial else "absent")
        link = t.phone_link if t else "none"
        msg: dict[str, Any] = {"type": "shuttle", "event": "phone", "adb": adb_state, "link": link}
        if t is not None:
            msg.update(name=t.phone.name if t.phone else "", host=t.current_host, phones=len(t.cands.hosts))
        await self.send_json(msg)

    # ------------------------------------------------------------------ main loop
    async def run(self) -> None:
        keepalive = asyncio.create_task(self._keepalive())
        try:
            async for msg in self.ws:
                self._last_activity = time.monotonic()
                if msg.type == WSMsgType.TEXT:
                    await self._on_text(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await self._on_binary(msg.data)
                elif msg.type == WSMsgType.ERROR:
                    log.warning("[%s] ctrl ws error: %s", self.device_id, self.ws.exception())
                    break
        finally:
            keepalive.cancel()
            for t in (self._task, self._tts_task):
                if t and not t.done():
                    t.cancel()

    async def _keepalive(self) -> None:
        v = self.cfg.voice
        while not self.ws.closed:
            await asyncio.sleep(v.keepalive_s)
            busy = (self._task and not self._task.done()) or self._listening
            if not busy and v.idle_close_s > 0 and time.monotonic() - self._last_activity > v.idle_close_s:
                log.info("[%s] ctrl channel idle for %.0fs, closing", self.device_id, v.idle_close_s)
                await self.ws.close(code=1000, message=b"idle")
                return
            await self.send_json({"type": "shuttle", "event": "ping"})

    async def _on_text(self, data: str) -> None:
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            log.warning("[%s] bad JSON: %.120s", self.device_id, data)
            return
        mtype = msg.get("type")
        if mtype == "hello":
            await self._on_hello(msg)
        elif mtype == "listen":
            await self._on_listen(msg)
        elif mtype == "abort":
            log.info("[%s] abort (%s)", self.device_id, msg.get("reason", ""))
            self._stop_tts()
        elif mtype == "mcp":
            log.debug("[%s] mcp from device: %.200s", self.device_id, data)
        else:
            log.debug("[%s] ignored message type %s", self.device_id, mtype)

    async def _on_hello(self, msg: dict[str, Any]) -> None:
        ver = msg.get("version")
        if isinstance(ver, int) and ver in (1, 2, 3):
            self.version = ver
        ap = msg.get("audio_params") or {}
        if isinstance(ap.get("sample_rate"), int) and ap["sample_rate"] != self.up_rate:
            self.up_rate = ap["sample_rate"]
            self._decoder = OpusDecoder(self.up_rate)
        log.info("[%s] hello v%s features=%s audio=%s", self.device_id, self.version, msg.get("features"), ap)
        await self.send_json({
            "type": "hello",
            "transport": "websocket",
            "session_id": self.session_id,
            "audio_params": {"format": "opus", "sample_rate": self.down_rate, "channels": 1,
                             "frame_duration": self.frame_ms},
        })
        self.hello_done = True
        await self.send_phone_status()

    async def _on_listen(self, msg: dict[str, Any]) -> None:
        state = msg.get("state")
        if state == "start":
            self.listen_mode = msg.get("mode", "manual")
            log.info("[%s] listen start (%s)", self.device_id, self.listen_mode)
            self._stop_tts()
            if self.cfg.voice.cancel_on_new_utterance and self._task and not self._task.done():
                log.info("[%s] new utterance cancels the running task", self.device_id)
                self._task.cancel()
            self._listening = True
            self._pcm = bytearray()
            self._voiced = False
            self._silence_ms = 0
        elif state == "stop":
            log.info("[%s] listen stop", self.device_id)
            self._finish_utterance()
        elif state == "detect":
            log.info("[%s] wake word: %s", self.device_id, msg.get("text"))

    async def _on_binary(self, data: bytes) -> None:
        if not self._listening:
            return
        if self.version == 2 and len(data) >= 16:
            size = struct.unpack(">I", data[12:16])[0]
            data = data[16:16 + size]
        elif self.version == 3 and len(data) >= 4:
            size = struct.unpack(">H", data[2:4])[0]
            data = data[4:4 + size]
        try:
            chunk = self._decoder.decode(data, self.up_rate * 120 // 1000)
        except RuntimeError as e:
            log.debug("[%s] opus decode error: %s", self.device_id, e)
            return
        self._pcm += chunk
        if pcmlib.duration_ms(bytes(self._pcm), self.up_rate) > self.cfg.voice.max_utterance_s * 1000:
            log.warning("[%s] utterance too long, cutting", self.device_id)
            self._finish_utterance()
            return
        if self.listen_mode in ("auto", "realtime"):
            # Minimal energy VAD for stock xiaozhi devices in auto mode.
            frame_ms = pcmlib.duration_ms(chunk, self.up_rate)
            if pcmlib.rms(chunk) > self.cfg.voice.vad_threshold:
                self._voiced = True
                self._silence_ms = 0
            elif self._voiced:
                self._silence_ms += frame_ms
                if self._silence_ms >= self.cfg.voice.vad_silence_ms:
                    self._finish_utterance()

    def _finish_utterance(self) -> None:
        if not self._listening:
            return
        self._listening = False
        pcm = bytes(self._pcm)
        self._pcm = bytearray()
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = asyncio.create_task(self._process(pcm), name=f"utterance-{self.device_id}")

    # ------------------------------------------------------------------ pipeline
    async def _process(self, pcm: bytes) -> None:
        ms = pcmlib.duration_ms(pcm, self.up_rate)
        if ms < self.cfg.voice.min_utterance_ms:
            log.info("[%s] utterance %d ms too short, ignored", self.device_id, ms)
            await self.status("idle")
            return
        t0 = time.monotonic()
        try:
            await self.status("thinking", "识别中…")
            text = await self.app.asr.transcribe(pcm, self.up_rate)
            self.last_text = text
            await self.send_json({"type": "stt", "text": text})
            if not text:
                await self.status("error", "没听清")
                await self.speak("没听清，请再说一遍")
                return
            if self.cfg.phones.voice_commands:
                answer = await self.app.phone_commands.handle_voice(text, self.device_id)
                if answer is not None:
                    self.last_reply = answer
                    log.info("[%s] phone command '%s' -> '%s'", self.device_id, text, answer)
                    await self.status("done", answer)
                    await self.speak(answer)
                    return
            await self.send_json({"type": "llm", "emotion": "thinking", "text": "🤔"})
            await self.status("thinking", text)

            ctx = AgentContext(
                phone=self.app.phone_for(self.device_id), report=self.status, device_id=self.device_id,
                max_steps=self.cfg.agent.max_steps, step_delay_s=self.cfg.agent.step_delay_s,
                phone_api_url=self.app.phone_api_url(self.device_id), phone_api_token=self.cfg.auth.token)
            reply = await asyncio.wait_for(self.app.agent.run(text, ctx), self.cfg.agent.timeout_s)
            reply = (reply or "完成了").strip()
            self.last_reply = reply
            log.info("[%s] '%s' -> '%s' (%.1fs)", self.device_id, text, reply, time.monotonic() - t0)
            await self.status("done", reply)
            await self.speak(reply)
        except asyncio.CancelledError:
            log.info("[%s] task cancelled", self.device_id)
            raise
        except PhoneUnavailable as e:
            log.warning("[%s] %s", self.device_id, e)
            await self.status("error", "手机未连接")
            await self.speak("手机没有连接")
        except asyncio.TimeoutError:
            await self.status("error", "执行超时")
            await self.speak("执行超时了")
        except Exception as e:  # keep the session alive whatever the agent does
            log.exception("[%s] pipeline failed", self.device_id)
            await self.status("error", f"出错：{str(e)[:40]}")
            await self.speak("出错了")

    def _stop_tts(self) -> None:
        if self._tts_task and not self._tts_task.done():
            self._tts_task.cancel()

    async def speak(self, text: str) -> None:
        self._stop_tts()
        task = asyncio.create_task(self._speak(text))
        self._tts_task = task
        try:
            await task
        except asyncio.CancelledError:
            me = asyncio.current_task()
            if me is not None and me.cancelling():
                raise  # we ourselves are being cancelled
            # only the TTS was aborted (device "abort" / new listen): not an error

    async def _speak(self, text: str) -> None:
        try:
            pcm, rate = await self.app.tts.synthesize(text)
        except Exception as e:
            log.error("[%s] TTS failed: %s", self.device_id, e)
            return
        pcm = pcmlib.resample(pcm, rate, self.down_rate)
        frame_samples = self.down_rate * self.frame_ms // 1000
        encoder = OpusEncoder(self.down_rate)
        packets = [encoder.encode(f, frame_samples) for f in pcmlib.frames(pcm, frame_samples)]
        await self.send_json({"type": "tts", "state": "start"})
        await self.send_json({"type": "tts", "state": "sentence_start", "text": text})
        await asyncio.sleep(0.1)  # let the device switch to "speaking" before audio arrives
        lead = self.cfg.voice.tts_lead_frames
        start = time.monotonic()
        try:
            for i, p in enumerate(packets):
                target = start + max(0, i - lead) * self.frame_ms / 1000
                delay = target - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
                await self.send_audio(p)
            # wait until the device has played everything before "stop"
            remaining = start + len(packets) * self.frame_ms / 1000 - time.monotonic()
            if remaining > 0:
                await asyncio.sleep(remaining)
        finally:
            await self.send_json({"type": "tts", "state": "stop"})
        log.info("[%s] spoke %d frames (%.1fs)", self.device_id, len(packets), len(packets) * self.frame_ms / 1000)
