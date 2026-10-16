"""aiohttp application: routes, shared services and lifecycle.

Routes (one port, put behind Cloudflare Tunnel):
  GET  /ws/ctrl            xiaozhi-compatible voice/control WebSocket   (token)
  GET  /ws/adb             ADB byte pass-through WebSocket              (token)
  POST /xiaozhi/ota/       xiaozhi OTA/config check (time sync, optional firmware)
  GET  /health             liveness for remote monitoring (no secrets, no token)
  GET  /status             detailed state                                (token)
  *    /api/phone/...      phone control API for external agents        (token)
  GET  /firmware/<file>    firmware download for OTA (if ota.firmware_dir is set)
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from aiohttp import web

from . import __version__
from .adb.adb_cli import AdbCli, AdbError
from .adb.phone import PhoneController, PhoneError, PhoneUnavailable
from .adb.tunnel import AdbTunnel, TunnelManager
from .auth import check_token
from .config import Config
from .pipeline.agent import create_agent
from .pipeline.asr import create_asr
from .pipeline.session import CtrlSession
from .pipeline.tts import create_tts

log = logging.getLogger(__name__)

APP_KEY = web.AppKey("shuttle", object)


class ShuttleApp:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.started = time.time()
        self.adb = AdbCli(cfg.adb.path, cfg.adb.command_timeout_s)
        self.tunnels = TunnelManager(self.adb, cfg.adb, on_state_change=self._on_tunnel_state)
        self.asr = create_asr(cfg.asr)
        self.tts = create_tts(cfg.tts, cfg.voice.tts_sample_rate)
        self.agent = create_agent(cfg.agent)
        self.sessions: dict[str, CtrlSession] = {}
        self._phones: dict[str, PhoneController] = {}
        self.adb_version = ""
        self.counters = {"ctrl_connections": 0, "adb_connections": 0, "auth_failures": 0}

    # ------------------------------------------------------------------ services
    def serial_for(self, device_id: str | None) -> str | None:
        t = self.tunnels.find(device_id)
        if t is not None and (t.device_connected or t.adb_state == "device"):
            return t.serial
        return self.cfg.adb.fallback_serial or None

    def phone_for(self, device_id: str | None) -> PhoneController:
        key = device_id or "_default"
        if key not in self._phones:
            self._phones[key] = PhoneController(self.adb, lambda: self.serial_for(device_id), self.cfg.phone)
        return self._phones[key]

    def phone_api_url(self, device_id: str) -> str:
        host = "127.0.0.1" if self.cfg.server.host in ("0.0.0.0", "") else self.cfg.server.host
        return f"http://{host}:{self.cfg.server.port}/api/phone?device={device_id}"

    async def _on_tunnel_state(self, tunnel: AdbTunnel) -> None:
        s = self.sessions.get(tunnel.device_id)
        if s is not None:
            await s.send_phone_status()

    async def startup(self) -> None:
        try:
            res = await self.adb.run("version", timeout_s=10)
            self.adb_version = res.text.splitlines()[0] if res.text else ""
            await self.adb.run("start-server", timeout_s=20)
            log.info("adb: %s", self.adb_version)
        except AdbError as e:
            log.error("adb not usable: %s", e)
        log.info("ASR=%s agent=%s TTS=%s", self.asr.name, self.agent.name, self.tts.name)

    async def shutdown(self) -> None:
        for s in list(self.sessions.values()):
            with contextlib.suppress(Exception):
                await s.ws.close(code=1001, message=b"server shutdown")
        await self.tunnels.stop_all()
        for svc in (self.asr, self.tts, self.agent):
            with contextlib.suppress(Exception):
                await svc.close()

    # ------------------------------------------------------------------ handlers
    def _auth(self, request: web.Request) -> None:
        if not check_token(request, self.cfg.auth):
            self.counters["auth_failures"] += 1
            log.warning("auth failure from %s on %s", request.headers.get("CF-Connecting-IP", request.remote),
                        request.path)
            raise web.HTTPUnauthorized(text="invalid or missing token\n")

    @staticmethod
    def _device_id(request: web.Request) -> str:
        dev = request.headers.get("Device-Id") or request.query.get("device") or "unknown"
        return re.sub(r"[^0-9A-Za-z:_-]", "", dev)[:64] or "unknown"

    async def handle_ctrl(self, request: web.Request) -> web.StreamResponse:
        self._auth(request)
        device_id = self._device_id(request)
        try:
            version = int(request.headers.get("Protocol-Version", "1"))
        except ValueError:
            version = 1
        ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        self.counters["ctrl_connections"] += 1
        session = CtrlSession(ws, self, device_id, request.headers.get("Client-Id", ""), version)
        old = self.sessions.get(device_id)
        self.sessions[device_id] = session
        if old is not None and not old.ws.closed:
            await old.ws.close(code=4000, message=b"replaced")
        log.info("[%s] ctrl connected (protocol v%d, %s)", device_id, version,
                 request.headers.get("CF-Connecting-IP", request.remote))
        try:
            await session.run()
        finally:
            if self.sessions.get(device_id) is session:
                del self.sessions[device_id]
            log.info("[%s] ctrl disconnected", device_id)
        return ws

    async def handle_adb(self, request: web.Request) -> web.StreamResponse:
        self._auth(request)
        device_id = self._device_id(request)
        tunnel = self.tunnels.get_or_create(device_id)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=16 * 1024 * 1024)
        await ws.prepare(request)
        self.counters["adb_connections"] += 1
        await tunnel.handle_ws(ws)
        return ws

    async def handle_ota(self, request: web.Request) -> web.Response:
        """xiaozhi firmware asks this at boot (Ota::CheckVersion). No secrets in the reply."""
        if not self.cfg.ota.enabled:
            raise web.HTTPNotFound()
        info: dict[str, Any] = {}
        if request.body_exists:
            with contextlib.suppress(Exception):
                info = json.loads(await request.text())
        device_id = self._device_id(request)
        current = (info.get("application") or {}).get("version", "") if isinstance(info, dict) else ""
        log.info("[%s] OTA check from %s (fw %s)", device_id, request.headers.get("User-Agent", "?"), current)
        reply: dict[str, Any] = {
            "server_time": {"timestamp": int(time.time() * 1000), "timezone_offset": self.cfg.ota.timezone_offset_min},
            "firmware": {"version": current or "0.0.0", "url": ""},
            # Empty object: firmware keeps the URL/token provisioned from Kconfig.
            "websocket": {},
        }
        if self.cfg.ota.expose_websocket_url and self.cfg.server.public_url:
            reply["websocket"] = {"url": self.cfg.server.public_url.rstrip("/") + "/ws/ctrl", "version": 1}
        latest = self._latest_firmware()
        if latest is not None:
            version, name = latest
            base = self.cfg.server.public_url.rstrip("/").replace("wss://", "https://").replace("ws://", "http://")
            reply["firmware"] = {"version": version, "url": f"{base}/firmware/{name}"}
        return web.json_response(reply)

    def _latest_firmware(self) -> tuple[str, str] | None:
        if not self.cfg.ota.firmware_dir:
            return None
        d = Path(self.cfg.ota.firmware_dir).expanduser()
        best = None
        for f in d.glob("*.bin"):
            m = re.match(r"^(\d+\.\d+\.\d+)", f.stem)
            if m:
                key = tuple(int(x) for x in m.group(1).split("."))
                if best is None or key > best[0]:
                    best = (key, m.group(1), f.name)
        return (best[1], best[2]) if best else None

    async def handle_firmware(self, request: web.Request) -> web.StreamResponse:
        if not self.cfg.ota.firmware_dir:
            raise web.HTTPNotFound()
        name = request.match_info["name"]
        if "/" in name or ".." in name:
            raise web.HTTPBadRequest()
        path = Path(self.cfg.ota.firmware_dir).expanduser() / name
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def handle_health(self, request: web.Request) -> web.Response:
        tunnels = list(self.tunnels.tunnels.values())
        body = {
            "status": "ok",
            "version": __version__,
            "uptime_s": int(time.time() - self.started),
            "adb": bool(self.adb_version),
            "devices_ctrl": len(self.sessions),
            "devices_adb": sum(1 for t in tunnels if t.device_connected),
            "phones_ready": sum(1 for t in tunnels if t.adb_state == "device"),
        }
        return web.json_response(body)

    async def handle_status(self, request: web.Request) -> web.Response:
        self._auth(request)
        return web.json_response({
            "version": __version__,
            "uptime_s": int(time.time() - self.started),
            "adb_version": self.adb_version,
            "providers": {"asr": self.asr.name, "agent": self.agent.name, "tts": self.tts.name},
            "counters": self.counters,
            "tunnels": [t.status() for t in self.tunnels.tunnels.values()],
            "sessions": [{"device_id": s.device_id, "session_id": s.session_id, "protocol": s.version,
                          "since": int(s.created), "last_text": s.last_text, "last_reply": s.last_reply}
                         for s in self.sessions.values()],
            "config": self.cfg.redacted(),
        })

    # ---- phone API (for out-of-process agents such as pi) ----
    def _phone(self, request: web.Request) -> PhoneController:
        self._auth(request)
        if not self.cfg.phone_api.enabled:
            raise web.HTTPNotFound()
        device = request.query.get("device") or None
        return self.phone_for(device)

    async def api_phone(self, request: web.Request) -> web.StreamResponse:
        phone = self._phone(request)
        action = request.match_info["action"]
        body: dict[str, Any] = {}
        if request.method == "POST" and request.body_exists:
            body = json.loads(await request.text() or "{}") or {}
        try:
            if action == "screenshot":
                shot = await phone.screenshot_info()
                return web.Response(body=shot.jpeg, content_type="image/jpeg", headers={
                    "X-Width": str(shot.width), "X-Height": str(shot.height),
                    "X-Device-Width": str(shot.device_width), "X-Device-Height": str(shot.device_height)})
            if action == "tap":
                await phone.tap(float(body["x"]), float(body["y"]))
            elif action == "long_press":
                await phone.long_press(float(body["x"]), float(body["y"]), int(body.get("duration_ms", 800)))
            elif action == "swipe":
                await phone.swipe(float(body["x1"]), float(body["y1"]), float(body["x2"]), float(body["y2"]),
                                  int(body.get("duration_ms", 300)))
            elif action == "scroll":
                await phone.scroll(body.get("direction", "down"))
            elif action == "text":
                return web.json_response({"ok": True, "method": await phone.input_text(str(body["text"]))})
            elif action == "back":
                await phone.back()
            elif action == "home":
                await phone.home()
            elif action == "enter":
                await phone.enter()
            elif action == "key":
                await phone.keyevent(int(body["code"]))
            elif action == "launch":
                await phone.launch_app(str(body["package"]))
            elif action == "ui":
                elems = await phone.ui_elements()
                return web.json_response({"elements": [
                    {"text": e.text, "desc": e.desc, "id": e.resource_id, "class": e.cls,
                     "clickable": e.clickable, "bounds": e.bounds, "center": e.center} for e in elems]})
            elif action == "ui_xml":
                return web.Response(text=await phone.dump_ui(), content_type="application/xml")
            elif action == "info":
                return web.json_response({"serial": phone.serial, "ready": await phone.is_ready(),
                                          "current_app": await phone.current_app()})
            else:
                raise web.HTTPNotFound(text=f"unknown action {action}\n")
        except PhoneUnavailable as e:
            return web.json_response({"ok": False, "error": str(e)}, status=503)
        except (PhoneError, AdbError, KeyError, ValueError, TypeError) as e:
            return web.json_response({"ok": False, "error": str(e)}, status=400)
        return web.json_response({"ok": True})


def build_app(cfg: Config) -> web.Application:
    shuttle = ShuttleApp(cfg)
    app = web.Application(client_max_size=8 * 1024 * 1024)
    app[APP_KEY] = shuttle
    app.router.add_get("/ws/ctrl", shuttle.handle_ctrl)
    app.router.add_get("/ws/adb", shuttle.handle_adb)
    for path in ("/xiaozhi/ota/", "/xiaozhi/ota"):
        app.router.add_route("*", path, shuttle.handle_ota)
    app.router.add_get("/health", shuttle.handle_health)
    app.router.add_get("/status", shuttle.handle_status)
    app.router.add_route("*", "/api/phone/{action}", shuttle.api_phone)
    app.router.add_get("/firmware/{name}", shuttle.handle_firmware)

    async def on_startup(_: web.Application) -> None:
        await shuttle.startup()

    async def on_cleanup(_: web.Application) -> None:
        await shuttle.shutdown()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app


def get_shuttle(app: web.Application) -> ShuttleApp:
    return app[APP_KEY]  # type: ignore[return-value]


async def serve(cfg: Config) -> None:
    app = build_app(cfg)
    runner = web.AppRunner(app, handle_signals=True)
    await runner.setup()
    site = web.TCPSite(runner, cfg.server.host, cfg.server.port)
    await site.start()
    log.info("Shuttle server %s listening on http://%s:%d", __version__, cfg.server.host, cfg.server.port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
