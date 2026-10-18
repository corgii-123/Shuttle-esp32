#!/usr/bin/env python3
"""Simulate the Shuttle ESP32 on a computer, to test the server without hardware.

It speaks exactly what the firmware speaks:
  * /ws/ctrl  xiaozhi protocol: hello -> listen start(manual) -> Opus 16 kHz/60 ms -> listen stop,
              then prints stt / shuttle status / tts and collects the TTS Opus audio;
  * /ws/adb   (with --phone HOST:PORT, repeatable) the TCP<->WebSocket byte bridge to a real phone's
              adbd: hello with the reachable phones, the server picks one, same lifecycle as the firmware.

Examples
  # whole chain with a WAV file, save the spoken reply
  python tools/sim_device.py --url ws://127.0.0.1:8780 --token T --wav hello.wav --save-tts reply.wav
  # macOS: synthesize the utterance with `say`, play the reply
  python tools/sim_device.py --url wss://shuttle.example.com --token T --say "打开设置" --play
  # be the ADB bridge for a phone on the same Wi-Fi (adb tcpip 5555 done), keep running
  python tools/sim_device.py --url ws://127.0.0.1:8780 --token T --phone 192.168.1.23:5555 --bridge-only
  # two phones on the Wi-Fi: the server chooses; say "换手机" (or POST /api/phones/next) to switch
  python tools/sim_device.py --token T --phone 192.168.1.23 --phone 192.168.1.42 --bridge-only
  # push-to-talk with the computer microphone (pip install sounddevice): Enter = start/stop
  python tools/sim_device.py --url ws://127.0.0.1:8780 --token T --mic --play

Needs: aiohttp, libopus (brew install opus); server/ must be importable (run from repo root).
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "server"))
from shuttle_server.audio import pcm as pcmlib  # noqa: E402
from shuttle_server.audio.opus import OpusDecoder, OpusEncoder  # noqa: E402

log = logging.getLogger("sim")

UP_RATE = 16000
FRAME_MS = 60


@dataclass
class TurnResult:
    stt: str = ""
    statuses: list[tuple[str, str]] = field(default_factory=list)
    tts_text: list[str] = field(default_factory=list)
    tts_pcm: bytearray = field(default_factory=bytearray)
    tts_rate: int = 24000
    tts_frames: int = 0
    phone: dict = field(default_factory=dict)
    elapsed_s: float = 0.0

    @property
    def final_state(self) -> str:
        return self.statuses[-1][0] if self.statuses else ""


def headers(token: str, device_id: str, channel: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Protocol-Version": "1",
        "Device-Id": device_id,
        "Client-Id": str(uuid.uuid5(uuid.NAMESPACE_DNS, device_id)),
        "X-Shuttle-Channel": channel,
    }


# ---------------------------------------------------------------------------- audio input
def load_utterance(args: argparse.Namespace) -> bytes:
    if args.wav:
        pcm, rate = pcmlib.read_wav(Path(args.wav).read_bytes())
        return pcmlib.resample(pcm, rate, UP_RATE)
    if args.say:
        if shutil.which("say"):
            with tempfile.TemporaryDirectory() as d:
                out = Path(d) / "u.wav"
                subprocess.run(["say", "-o", str(out), f"--data-format=LEI16@{UP_RATE}", "--file-format=WAVE",
                                "--", args.say], check=True)
                pcm, rate = pcmlib.read_wav(out.read_bytes())
                return pcmlib.resample(pcm, rate, UP_RATE)
        log.warning("`say` not available: sending 1.5 s of tone instead (use asr.provider: mock)")
    return pcmlib.tone(300, 1500, UP_RATE, 0.3)


def record_mic(seconds_max: float = 30.0) -> bytes:
    import sounddevice as sd  # optional dependency

    print("录音中... 按 Enter 结束")
    frames: list[bytes] = []
    stream = sd.RawInputStream(samplerate=UP_RATE, channels=1, dtype="int16",
                               callback=lambda data, n, t, s: frames.append(bytes(data)))
    with stream:
        start = time.time()
        sys.stdin.readline()
        if time.time() - start > seconds_max:
            pass
    return b"".join(frames)


def play_pcm(pcm: bytes, rate: int) -> None:
    try:
        import sounddevice as sd

        import array
        sd.play(array.array("h", pcm), rate, blocking=True)
        return
    except ImportError:
        pass
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        f.write(pcmlib.wav_bytes(pcm, rate))
        path = f.name
    player = shutil.which("afplay") or shutil.which("aplay") or shutil.which("ffplay")
    if player:
        argv = [player, path] if "ffplay" not in player else [player, "-autoexit", "-nodisp", path]
        subprocess.run(argv, check=False)
    else:
        print(f"(no audio player found; reply saved to {path})")


# ---------------------------------------------------------------------------- ctrl channel
class CtrlClient:
    def __init__(self, session: aiohttp.ClientSession, url: str, token: str, device_id: str):
        self.http = session
        self.url = url.rstrip("/") + "/ws/ctrl"
        self.token = token
        self.device_id = device_id
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.session_id = ""
        self.down_rate = 24000
        self.queue: asyncio.Queue = asyncio.Queue()
        self._reader: asyncio.Task | None = None

    async def connect(self) -> None:
        self.ws = await self.http.ws_connect(self.url, headers=headers(self.token, self.device_id, "ctrl"),
                                             heartbeat=None, max_msg_size=8 * 1024 * 1024)
        await self.ws.send_str(json.dumps({
            "type": "hello", "version": 1, "features": {"mcp": True}, "transport": "websocket",
            "audio_params": {"format": "opus", "sample_rate": UP_RATE, "channels": 1, "frame_duration": FRAME_MS},
        }))
        msg = await asyncio.wait_for(self.ws.receive(), 10)
        hello = json.loads(msg.data)
        if hello.get("type") != "hello" or hello.get("transport") != "websocket":
            raise RuntimeError(f"bad server hello: {hello}")
        self.session_id = hello.get("session_id", "")
        self.down_rate = int((hello.get("audio_params") or {}).get("sample_rate", 24000))
        log.info("ctrl connected, session %s, downlink %d Hz", self.session_id, self.down_rate)
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.ws
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self.queue.put(("json", json.loads(msg.data)))
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await self.queue.put(("audio", msg.data))
        await self.queue.put(("closed", None))

    async def send(self, obj: dict) -> None:
        obj.setdefault("session_id", self.session_id)
        assert self.ws
        await self.ws.send_str(json.dumps(obj, ensure_ascii=False))

    async def utterance(self, pcm: bytes, realtime: bool = True, timeout_s: float = 300.0) -> TurnResult:
        """Push-to-talk turn: listen start -> audio -> listen stop -> wait for the reply."""
        assert self.ws
        res = TurnResult(tts_rate=self.down_rate)
        t0 = time.monotonic()
        enc = OpusEncoder(UP_RATE, application=2048)
        frame = UP_RATE * FRAME_MS // 1000
        await self.send({"type": "listen", "state": "start", "mode": "manual"})
        start = time.monotonic()
        for i, f in enumerate(pcmlib.frames(pcm, frame)):
            await self.ws.send_bytes(enc.encode(f, frame))
            if realtime:
                delay = start + (i + 1) * FRAME_MS / 1000 - time.monotonic()
                if delay > 0:
                    await asyncio.sleep(delay)
        await self.send({"type": "listen", "state": "stop"})
        log.info("sent %.1fs of audio", len(pcm) / 2 / UP_RATE)

        dec = OpusDecoder(self.down_rate)
        tts_started = False
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                kind, data = await asyncio.wait_for(self.queue.get(), max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                log.warning("timeout waiting for the reply")
                break
            if kind == "closed":
                log.warning("server closed the ctrl channel")
                break
            if kind == "audio":
                if tts_started:
                    res.tts_frames += 1
                    res.tts_pcm += dec.decode(data)
                continue
            t = data.get("type")
            if t == "stt":
                res.stt = data.get("text", "")
                print(f"  >> 识别: {res.stt}")
            elif t == "shuttle" and data.get("event") == "status":
                res.statuses.append((data.get("state", ""), data.get("text", "")))
                print(f"  [{data.get('state')}] {data.get('text', '')}")
                if data.get("state") == "idle":
                    break
            elif t == "shuttle" and data.get("event") == "phone":
                res.phone = data
                log.info("phone: adb=%s link=%s", data.get("adb"), data.get("link"))
            elif t == "tts":
                if data.get("state") == "start":
                    tts_started = True
                elif data.get("state") == "sentence_start":
                    res.tts_text.append(data.get("text", ""))
                    print(f"  << 播报: {data.get('text')}")
                elif data.get("state") == "stop":
                    break
        res.elapsed_s = time.monotonic() - t0
        return res

    async def close(self) -> None:
        if self.ws and not self.ws.closed:
            await self.ws.close()
        if self._reader:
            self._reader.cancel()


# ---------------------------------------------------------------------------- ADB bridge
CLOSE_SWITCH = 4005


def split_hostport(addr: str, default_port: int = 5555) -> tuple[str, int]:
    host, sep, port = addr.rpartition(":")
    if not sep or not port.isdigit():
        return addr, default_port
    return host, int(port)


class AdbBridgeSim:
    """Same behaviour as firmware/extra_components/shuttle/adb_bridge.cc + phone_scanner.cc.

    `phones` are the addresses this "device" can see (HOST or HOST:PORT; the real firmware finds
    them by scanning its Wi-Fi). A scan keeps the ones that accept a TCP connection. Each session:
    hello(candidates) -> server says connect(host) -> splice bytes. Close code 4005 = switch now.
    """

    def __init__(self, session: aiohttp.ClientSession, url: str, token: str, device_id: str,
                 phone: str | list[str], *, ssid: str = "sim-wifi", gateway: str = "", hello: bool = True,
                 scan_interval_s: float = 30.0):
        self.http = session
        self.url = url.rstrip("/") + "/ws/adb"
        self.token = token
        self.device_id = device_id
        self.phones = [phone] if isinstance(phone, str) else list(phone)
        self.ssid = ssid
        self.gateway = gateway
        self.hello = hello  # False = old firmware (dials phones[0], no hello)
        self.scan_interval_s = scan_interval_s
        self.candidates: list[str] = []
        self.last = ""
        self.current = ""
        self.phone_name = ""
        self.backoff = 1.0
        self.close_code: int | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None

    async def scan(self) -> list[str]:
        async def probe(addr: str) -> bool:
            host, port = split_hostport(addr)
            try:
                _, w = await asyncio.wait_for(asyncio.open_connection(host, port), 1.0)
            except (OSError, asyncio.TimeoutError):
                return False
            w.close()
            return True

        ok = await asyncio.gather(*(probe(a) for a in self.phones))
        found = [a for a, good in zip(self.phones, ok) if good]
        if found != self.candidates:
            log.info("scan: %s", found)
        self.candidates = found
        return found

    async def rescan(self) -> None:
        """Scan again and report the result on the open session (like the firmware's idle rescan)."""
        before = list(self.candidates)
        await self.scan()
        if self._ws is not None and not self._ws.closed and self.candidates != before:
            await self._ws.send_str(json.dumps({"type": "candidates", "ssid": self.ssid, "candidates": self.candidates}))

    async def run_forever(self) -> None:
        while True:
            lasted = await self.run_once()
            if self.close_code == CLOSE_SWITCH:
                delay, self.backoff = 0.05, 1.0
            elif lasted is None or lasted < 10:
                delay = self.backoff * random.uniform(0.8, 1.2)
                self.backoff = min(30.0, self.backoff * 2)
            else:
                delay, self.backoff = 0.3, 1.0
            await asyncio.sleep(delay)

    async def _pick(self, ws: aiohttp.ClientWebSocketResponse) -> str | None:
        if not self.hello:
            return self.phones[0] if self.phones else None
        await ws.send_str(json.dumps({"type": "hello", "v": 2, "ssid": self.ssid, "gateway": self.gateway,
                                      "ip": "127.0.0.1", "candidates": self.candidates, "last": self.last,
                                      "fixed": False}))
        deadline = time.monotonic() + 3.0
        while (left := deadline - time.monotonic()) > 0:
            try:
                msg = await ws.receive(timeout=left)
            except asyncio.TimeoutError:
                break
            if msg.type != aiohttp.WSMsgType.TEXT:
                return None
            data = json.loads(msg.data)
            if data.get("type") == "connect":
                return str(data.get("host"))
            if data.get("type") == "idle":
                return None
        # old server: our own choice
        return self.last if self.last in self.candidates else self.candidates[0]

    async def run_once(self) -> float | None:
        self.close_code = None
        if self.hello and not self.candidates:
            await self.scan()
            if not self.candidates:
                log.info("no phone found (scanned %s)", self.phones)
                return None  # like the firmware: no WebSocket until a phone is found
        try:
            ws = await self.http.ws_connect(self.url, headers=headers(self.token, self.device_id, "adb"),
                                            heartbeat=10, max_msg_size=16 * 1024 * 1024)
        except (aiohttp.ClientError, OSError) as e:
            log.warning("adb ws connect failed: %s", e)
            return None
        target = await self._pick(ws)
        if target is None:
            with contextlib.suppress(Exception):
                await ws.close()
            self.close_code = ws.close_code
            return None
        host, port = split_hostport(target)
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
        except (OSError, asyncio.TimeoutError) as e:
            log.warning("phone %s unreachable: %s", target, e)
            await ws.send_str(json.dumps({"type": "phone", "state": "unreachable", "host": target, "detail": str(e)}))
            await ws.close(code=4001, message=b"phone unreachable")
            if target in self.candidates:
                self.candidates.remove(target)
            return None
        self.current = self.last = target
        status = {"type": "phone", "state": "connected", "detail": f"{host}:{port}"}
        if self.hello:
            status["host"] = target
        await ws.send_str(json.dumps(status))
        log.info("adb bridge active: phone %s <-> %s", target, self.url)
        started = time.monotonic()
        self._ws = ws

        async def phone_to_ws() -> None:
            while True:
                data = await reader.read(16384)
                if not data:
                    return
                await ws.send_bytes(data)

        async def ws_to_phone() -> None:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type == aiohttp.WSMsgType.TEXT:
                    log.info("server control: %s", msg.data[:200])
                    data = json.loads(msg.data)
                    if data.get("type") == "phone_info":
                        self.phone_name = data.get("name", "")
                    elif data.get("type") == "rescan":
                        asyncio.create_task(self.rescan())

        async def periodic_scan() -> None:
            while True:
                await asyncio.sleep(self.scan_interval_s)
                await self.rescan()

        tasks = [asyncio.create_task(phone_to_ws()), asyncio.create_task(ws_to_phone())]
        scanner = asyncio.create_task(periodic_scan()) if self.hello else None
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for t in tasks + ([scanner] if scanner else []):
            t.cancel()
        writer.close()
        with contextlib.suppress(Exception):
            await ws.close()
        self._ws = None
        self.current = ""
        self.close_code = ws.close_code
        lasted = time.monotonic() - started
        log.info("adb session ended after %.1fs (close code %s)", lasted, self.close_code)
        return lasted


# ---------------------------------------------------------------------------- main
async def amain(args: argparse.Namespace) -> int:
    device_id = args.device_id
    async with aiohttp.ClientSession() as http:
        bridge_task = None
        if args.phone:
            bridge = AdbBridgeSim(http, args.url, args.token, device_id, args.phone, gateway=args.gateway,
                                  hello=not args.legacy_bridge)
            bridge_task = asyncio.create_task(bridge.run_forever())
        if args.bridge_only:
            if not bridge_task:
                print("--bridge-only needs --phone", file=sys.stderr)
                return 2
            await bridge_task
            return 0
        if bridge_task:
            await asyncio.sleep(2)  # let the tunnel come up

        ctrl = CtrlClient(http, args.url, args.token, device_id)
        await ctrl.connect()
        rc = 0
        try:
            turns = 0
            while True:
                if args.mic:
                    input("按 Enter 开始说话（Ctrl-C 退出）")
                    pcm = await asyncio.to_thread(record_mic)
                else:
                    pcm = load_utterance(args)
                result = await ctrl.utterance(pcm, realtime=not args.fast, timeout_s=args.timeout)
                turns += 1
                secs = len(result.tts_pcm) / 2 / result.tts_rate
                print(f"== 结果: state={result.final_state} stt={result.stt!r} tts={secs:.1f}s "
                      f"({result.tts_frames} frames) in {result.elapsed_s:.1f}s")
                if args.save_tts and result.tts_pcm:
                    Path(args.save_tts).write_bytes(pcmlib.wav_bytes(bytes(result.tts_pcm), result.tts_rate))
                    print(f"   reply audio saved to {args.save_tts}")
                if args.play and result.tts_pcm:
                    await asyncio.to_thread(play_pcm, bytes(result.tts_pcm), result.tts_rate)
                if result.final_state not in ("done", "idle"):
                    rc = 1
                if not args.mic and turns >= args.repeat:
                    break
        finally:
            await ctrl.close()
            if bridge_task:
                bridge_task.cancel()
        return rc


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=os.environ.get("SHUTTLE_URL", "ws://127.0.0.1:8780"))
    ap.add_argument("--token", default=os.environ.get("SHUTTLE_TOKEN", "change-me"))
    ap.add_argument("--device-id", default="sim:00:00:00:00:01")
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--wav", help="utterance WAV file (any rate, mono/stereo)")
    src.add_argument("--say", help="text spoken with macOS `say` as the utterance")
    src.add_argument("--mic", action="store_true", help="push-to-talk with the microphone (sounddevice)")
    ap.add_argument("--phone", action="append", metavar="HOST[:PORT]",
                    help="a phone adbd the simulated board can see; repeat for several phones (the server picks)")
    ap.add_argument("--gateway", default="", help="which --phone is the hotspot (DHCP gateway)")
    ap.add_argument("--legacy-bridge", action="store_true", help="old firmware behaviour: no hello, first --phone")
    ap.add_argument("--bridge-only", action="store_true", help="only run the ADB bridge")
    ap.add_argument("--save-tts", help="write the reply audio to this WAV")
    ap.add_argument("--play", action="store_true", help="play the reply audio")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--fast", action="store_true", help="send audio faster than real time")
    ap.add_argument("--timeout", type=float, default=300.0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
