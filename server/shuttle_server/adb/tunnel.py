"""ADB tunnel: device WebSocket (/ws/adb)  <->  local TCP port  <->  native `adb`.

The ESP32 splices one TCP connection to the phone's adbd with one WebSocket. On the Mac we
listen on 127.0.0.1:<port> and run `adb connect 127.0.0.1:<port>`; each TCP connection that
adb opens is paired with one fresh device WebSocket session. A session is single-use: when
either side closes, the other is closed too and the device reconnects (new session), so the
phone always sees a fresh TCP connection for a fresh ADB handshake.

    adb server ──TCP──> 127.0.0.1:15555 ──(this file)──> WS /ws/adb ──> ESP32 ──TCP──> phone:5555
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from aiohttp import WSMsgType, web

from ..config import AdbConfig
from .adb_cli import AdbCli, AdbError

log = logging.getLogger(__name__)

MAX_PREPAIR_BUFFER = 1024 * 1024  # bytes from the phone before adb connected (normally 0)
READ_CHUNK = 64 * 1024

StateCallback = Callable[["AdbTunnel"], Awaitable[None] | None]


@dataclass
class WsSession:
    ws: web.WebSocketResponse
    id: int
    created: float = field(default_factory=time.monotonic)
    phone_ready: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    used: bool = False
    adb_writer: asyncio.StreamWriter | None = None
    prepair: bytearray = field(default_factory=bytearray)
    bytes_to_phone: int = 0
    bytes_from_phone: int = 0


class AdbTunnel:
    def __init__(self, device_id: str, port: int, adb: AdbCli, cfg: AdbConfig,
                 on_state_change: StateCallback | None = None):
        self.device_id = device_id
        self.port = port
        self.adb = adb
        self.cfg = cfg
        self.serial = f"127.0.0.1:{port}"
        self.on_state_change = on_state_change

        self.adb_state = "absent"  # from `adb devices`
        self.phone_link = "none"  # none | connecting | connected | unreachable (device reports)
        self.phone_detail = ""
        self.sessions_total = 0
        self.last_seen = 0.0

        self._server: asyncio.base_events.Server | None = None
        self._supervisor: asyncio.Task | None = None
        self._teardown: asyncio.Task | None = None
        self._session: WsSession | None = None
        self._session_changed = asyncio.Condition()
        self._next_id = 1
        self._offline_since: float | None = None

    # ------------------------------------------------------------------ lifecycle
    @property
    def active(self) -> bool:
        return self._server is not None

    @property
    def paired(self) -> bool:
        return self._session is not None and self._session.adb_writer is not None

    @property
    def device_connected(self) -> bool:
        return self._session is not None and not self._session.closed.is_set()

    def status(self) -> dict:
        s = self._session
        return {
            "device_id": self.device_id,
            "serial": self.serial,
            "listening": self.active,
            "device_connected": self.device_connected,
            "paired": self.paired,
            "phone_link": self.phone_link,
            "phone_detail": self.phone_detail,
            "adb_state": self.adb_state,
            "sessions_total": self.sessions_total,
            "current_session": None if s is None else {
                "id": s.id, "age_s": round(time.monotonic() - s.created, 1),
                "to_phone": s.bytes_to_phone, "from_phone": s.bytes_from_phone,
            },
        }

    async def ensure_started(self) -> None:
        if self._teardown is not None:
            self._teardown.cancel()
            self._teardown = None
        if self._server is None:
            self._server = await asyncio.start_server(self._on_adb_conn, self.cfg.bind_host, self.port)
            log.info("[%s] ADB tunnel listening on %s:%d", self.device_id, self.cfg.bind_host, self.port)
        if self._supervisor is None or self._supervisor.done():
            self._supervisor = asyncio.create_task(self._supervise(), name=f"adb-supervisor-{self.port}")

    async def stop(self) -> None:
        """Close everything and remove the adb entry (device gone for good / shutdown)."""
        if self._supervisor:
            self._supervisor.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._supervisor
            self._supervisor = None
        if self._session:
            await self._close_session(self._session, 1001, "server shutdown")
        if self._server:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
            self._server = None
        with contextlib.suppress(AdbError):
            out = await self.adb.disconnect(self.serial)
            log.info("[%s] adb disconnect %s: %s", self.device_id, self.serial, out)
        await self._set_adb_state("absent")
        log.info("[%s] ADB tunnel stopped", self.device_id)

    def _schedule_teardown(self) -> None:
        if self._teardown is None or self._teardown.done():
            self._teardown = asyncio.create_task(self._teardown_later())

    async def _teardown_later(self) -> None:
        await asyncio.sleep(self.cfg.device_grace_s)
        if not self.device_connected:
            log.info("[%s] device gone for %.0fs, cleaning up", self.device_id, self.cfg.device_grace_s)
            await self.stop()

    async def _notify(self) -> None:
        if self.on_state_change:
            res = self.on_state_change(self)
            if asyncio.iscoroutine(res):
                with contextlib.suppress(Exception):
                    await res

    async def _set_adb_state(self, state: str) -> None:
        if state != self.adb_state:
            log.info("[%s] adb %s: %s -> %s", self.device_id, self.serial, self.adb_state, state)
            self.adb_state = state
            await self._notify()

    # ------------------------------------------------------------------ device side
    async def handle_ws(self, ws: web.WebSocketResponse) -> None:
        """Serve one /ws/adb connection from the device until it closes."""
        await self.ensure_started()
        session = WsSession(ws=ws, id=self._next_id)
        self._next_id += 1
        self.sessions_total += 1
        self.last_seen = time.time()
        old = self._session
        async with self._session_changed:
            self._session = session
            self._session_changed.notify_all()
        if old is not None:
            await self._close_session(old, 4000, "replaced by new session")
        self.phone_link = "connecting"
        await self._notify()
        log.info("[%s] device ADB session #%d connected", self.device_id, session.id)
        fallback = asyncio.create_task(self._phone_ready_fallback(session))
        try:
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    await self._from_phone(session, msg.data)
                elif msg.type == WSMsgType.TEXT:
                    await self._on_control(session, msg.data)
                elif msg.type == WSMsgType.ERROR:
                    log.warning("[%s] ADB ws error: %s", self.device_id, ws.exception())
                    break
        finally:
            fallback.cancel()
            session.closed.set()
            if session.adb_writer is not None:
                session.adb_writer.close()
            log.info("[%s] device ADB session #%d closed (to phone %d B, from phone %d B)", self.device_id,
                     session.id, session.bytes_to_phone, session.bytes_from_phone)
            async with self._session_changed:
                if self._session is session:
                    self._session = None
                    if self.phone_link == "connected":
                        self.phone_link = "none"
                self._session_changed.notify_all()
            await self._notify()
            if not self.device_connected:
                self._schedule_teardown()

    async def _phone_ready_fallback(self, session: WsSession) -> None:
        # Devices that do not send the optional status frame are treated as ready.
        await asyncio.sleep(self.cfg.phone_ready_timeout_s)
        if not session.phone_ready.is_set() and not session.closed.is_set():
            log.debug("[%s] no phone status frame, assuming phone connected", self.device_id)
            session.phone_ready.set()
            async with self._session_changed:
                self._session_changed.notify_all()

    async def _on_control(self, session: WsSession, data: str) -> None:
        try:
            msg = json.loads(data)
        except json.JSONDecodeError:
            log.warning("[%s] bad control frame: %.100s", self.device_id, data)
            return
        if msg.get("type") != "phone":
            return
        state = msg.get("state", "")
        self.phone_detail = str(msg.get("detail", ""))
        log.info("[%s] phone link: %s %s", self.device_id, state, self.phone_detail)
        if state == "connected":
            self.phone_link = "connected"
            session.phone_ready.set()
            async with self._session_changed:
                self._session_changed.notify_all()
        elif state == "unreachable":
            self.phone_link = "unreachable"
        await self._notify()

    async def _from_phone(self, session: WsSession, data: bytes) -> None:
        session.bytes_from_phone += len(data)
        writer = session.adb_writer
        if writer is None:
            if len(session.prepair) + len(data) > MAX_PREPAIR_BUFFER:
                log.warning("[%s] dropping %d phone bytes (adb not attached)", self.device_id, len(data))
                return
            session.prepair += data
            return
        try:
            writer.write(data)
            await writer.drain()
        except (ConnectionError, RuntimeError) as e:
            log.info("[%s] write to adb failed: %r", self.device_id, e)
            await self._close_session(session, 1000, "adb write failed")

    async def _close_session(self, session: WsSession, code: int, reason: str) -> None:
        if session.adb_writer is not None:
            session.adb_writer.close()
        if not session.ws.closed:
            with contextlib.suppress(Exception):
                await session.ws.close(code=code, message=reason.encode())
        session.closed.set()

    # ------------------------------------------------------------------ adb side
    async def _wait_for_session(self) -> WsSession | None:
        deadline = time.monotonic() + self.cfg.pair_wait_s
        async with self._session_changed:
            while True:
                s = self._session
                if s is not None and not s.closed.is_set() and not s.used and s.phone_ready.is_set():
                    return s
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._session_changed.wait(), remaining)

    async def _on_adb_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        current = self._session
        if current is not None and current.used:
            # adb opened a new transport, so the old one is dead from its point of view.
            log.info("[%s] new adb connection %s replaces paired session #%d", self.device_id, peer, current.id)
            await self._close_session(current, 4002, "adb reconnected")
        session = await self._wait_for_session()
        if session is None:
            log.info("[%s] adb connection %s: no device session within %.0fs, closing", self.device_id, peer,
                     self.cfg.pair_wait_s)
            writer.close()
            return
        session.used = True
        session.adb_writer = writer
        log.info("[%s] adb %s paired with device session #%d", self.device_id, peer, session.id)
        if session.prepair:
            writer.write(bytes(session.prepair))
            session.prepair.clear()
        try:
            while not session.closed.is_set():
                data = await reader.read(READ_CHUNK)
                if not data:
                    break
                session.bytes_to_phone += len(data)
                await session.ws.send_bytes(data)
        except (ConnectionError, RuntimeError, asyncio.CancelledError) as e:
            log.debug("[%s] adb pump ended: %r", self.device_id, e)
        finally:
            log.info("[%s] adb side of session #%d closed", self.device_id, session.id)
            await self._close_session(session, 1000, "adb closed")
            with contextlib.suppress(Exception):
                writer.close()

    # ------------------------------------------------------------------ supervisor
    async def _supervise(self) -> None:
        """Keep `adb connect` alive while a device is attached; reset wedged transports."""
        while True:
            try:
                state = await self.adb.get_state(self.serial)
                await self._set_adb_state(state)
                if self.device_connected:
                    if state == "offline":
                        # paired: transport wedged; unpaired: adb's own reconnect may have given up.
                        self._offline_since = self._offline_since or time.monotonic()
                        if time.monotonic() - self._offline_since > self.cfg.offline_reset_s:
                            log.warning("[%s] adb offline for %.0fs, resetting transport", self.device_id,
                                        self.cfg.offline_reset_s)
                            await self.adb.disconnect(self.serial)
                            if self.paired and self._session:
                                await self._close_session(self._session, 4003, "adb offline reset")
                            self._offline_since = None
                        elif not self.paired:
                            out = await self.adb.connect(self.serial)
                            log.info("[%s] adb connect %s: %s", self.device_id, self.serial, out)
                    else:
                        self._offline_since = None
                        if state == "absent" and not self.paired:
                            out = await self.adb.connect(self.serial)
                            log.info("[%s] adb connect %s: %s", self.device_id, self.serial, out)
                else:
                    self._offline_since = None
            except AdbError as e:
                log.warning("[%s] adb supervisor: %s", self.device_id, e)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[%s] adb supervisor crashed (continuing)", self.device_id)
            await asyncio.sleep(self.cfg.connect_interval_s)


class TunnelManager:
    """One tunnel per Device-Id; local ports allocated from adb.local_port upwards."""

    def __init__(self, adb: AdbCli, cfg: AdbConfig, on_state_change: StateCallback | None = None):
        self.adb = adb
        self.cfg = cfg
        self.on_state_change = on_state_change
        self.tunnels: dict[str, AdbTunnel] = {}
        self.last_active: str | None = None

    def get_or_create(self, device_id: str) -> AdbTunnel:
        t = self.tunnels.get(device_id)
        if t is None:
            used = {x.port for x in self.tunnels.values()}
            port = self.cfg.local_port
            while port in used:
                port += 1
            t = AdbTunnel(device_id, port, self.adb, self.cfg, self.on_state_change)
            self.tunnels[device_id] = t
        self.last_active = device_id
        return t

    def find(self, device_id: str | None) -> AdbTunnel | None:
        if device_id and device_id in self.tunnels:
            return self.tunnels[device_id]
        if self.last_active and self.last_active in self.tunnels:
            return self.tunnels[self.last_active]
        return None

    async def stop_all(self) -> None:
        for t in list(self.tunnels.values()):
            await t.stop()
