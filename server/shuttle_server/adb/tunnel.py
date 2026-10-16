"""ADB tunnel: device WebSocket (/ws/adb)  <->  local TCP port  <->  native `adb`.

The ESP32 splices one TCP connection to the phone's adbd with one WebSocket. On the Mac we
listen on 127.0.0.1:<port> and run `adb connect 127.0.0.1:<port>`; each TCP connection that
adb opens is paired with one fresh device WebSocket session. A session is single-use: when
either side closes, the other is closed too and the device reconnects (new session), so the
phone always sees a fresh TCP connection for a fresh ADB handshake.

    adb server ──TCP──> 127.0.0.1:15555 ──(this file)──> WS /ws/adb ──> ESP32 ──TCP──> phone:5555

Several phones: the device starts each session with {"type":"hello","candidates":[...]} (the adbd
hosts it found on its Wi-Fi); we answer {"type":"connect","host":...} (see phones.py for the
policy), identify the phone once adb is up and tell the device its name ({"type":"phone_info"}).
Switching = close the session with code 4005; the device reconnects at once and asks again.
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

from ..config import AdbConfig, PhonesConfig
from .adb_cli import AdbCli, AdbError
from .phones import IDENTIFY_CMD, Candidates, PhoneRecord, PhoneRegistry, choose_host, next_host, parse_identity

log = logging.getLogger(__name__)

MAX_PREPAIR_BUFFER = 1024 * 1024  # bytes from the phone before adb connected (normally 0)
READ_CHUNK = 64 * 1024

StateCallback = Callable[["AdbTunnel"], Awaitable[None] | None]
BusyCallback = Callable[[str], bool]

CLOSE_SWITCH = 4005  # server -> device: reconnect now and ask which phone (no backoff)


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
    hello: bool = False  # device speaks the multi-phone protocol
    target: str = ""  # host we told the device to dial
    end_code: int = 1000  # close code when the adb side ends the session


class AdbTunnel:
    def __init__(self, device_id: str, port: int, adb: AdbCli, cfg: AdbConfig,
                 on_state_change: StateCallback | None = None, registry: PhoneRegistry | None = None,
                 phones_cfg: PhonesConfig | None = None, is_busy: BusyCallback | None = None):
        self.device_id = device_id
        self.port = port
        self.adb = adb
        self.cfg = cfg
        self.serial = f"127.0.0.1:{port}"
        self.on_state_change = on_state_change
        self.registry = registry or PhoneRegistry()
        self.pcfg = phones_cfg or PhonesConfig()
        self.is_busy = is_busy or (lambda _device_id: False)

        # multi-phone state (survives device reconnects; the tunnel object lives as long as the server)
        self.cands = Candidates()
        self.current_host = ""  # phone IP the device is spliced to
        self.phone: PhoneRecord | None = None  # identified phone behind the tunnel
        self.phone_generation = 0  # bumps whenever the phone behind the serial may have changed
        self._seen_hosts: set[str] = set()
        self._avoid: dict[str, float] = {}
        self._requested: str | None = None
        self._identified_session = -1
        self._cands_changed = asyncio.Event()

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
            "phone": self.phone.public() if self.phone else None,
            "phone_host": self.current_host,
            "candidates": self.candidate_list(),
            "ssid": self.cands.ssid,
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

    def candidate_list(self) -> list[dict]:
        out = []
        for h in self.cands.hosts:
            rec = self.registry.by_host(h, self.cands.ssid)
            out.append({"host": h, "current": h == self.current_host, "hotspot": h == self.cands.gateway,
                        "serial": rec.serial if rec else None, "name": rec.name if rec else None})
        return out

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
            if self._session is None and session.target:
                self.current_host = ""
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
        mtype = msg.get("type")
        if mtype == "hello":
            await self._on_hello(session, msg)
            return
        if mtype == "candidates":
            await self._on_candidates(session, msg)
            return
        if mtype != "phone":
            return
        state = msg.get("state", "")
        self.phone_detail = str(msg.get("detail", ""))
        log.info("[%s] phone link: %s %s", self.device_id, state, self.phone_detail)
        if state == "connected":
            self.phone_link = "connected"
            host = str(msg.get("host") or "") or self.phone_detail.rpartition(":")[0] or session.target
            if host and host != self.current_host:
                self.current_host = host
                self.phone_generation += 1
                # best guess until adb identifies it
                self.phone = self.registry.by_host(host, self.cands.ssid)
            session.phone_ready.set()
            async with self._session_changed:
                self._session_changed.notify_all()
        elif state == "unreachable":
            self.phone_link = "unreachable"
            bad = str(msg.get("host") or "") or session.target
            if bad:
                self._avoid[bad] = time.monotonic() + self.pcfg.avoid_after_fail_s
        await self._notify()

    # ---- multi-phone protocol
    def _update_candidates(self, msg: dict) -> list[str]:
        """Store the device's report; returns hosts that are new on this Wi-Fi since the last report."""
        hosts = [str(h) for h in (msg.get("candidates") or []) if isinstance(h, str) and h][:32]
        ssid = str(msg.get("ssid", self.cands.ssid) or "")
        new_network = ssid != self.cands.ssid or not self._seen_hosts
        self.cands = Candidates(
            ssid=ssid, gateway=str(msg.get("gateway", self.cands.gateway) or ""),
            ip=str(msg.get("ip", self.cands.ip) or ""), hosts=hosts,
            last=str(msg.get("last", self.cands.last) or ""), fixed=bool(msg.get("fixed", self.cands.fixed)),
            updated=time.time())
        if new_network:
            # first report on this network: nothing counts as "just appeared"
            self._seen_hosts = set(hosts)
            new: list[str] = []
        else:
            new = [h for h in hosts if h not in self._seen_hosts]
            self._seen_hosts |= set(hosts)
        self._cands_changed.set()
        return new

    def _avoided(self) -> set[str]:
        now = time.monotonic()
        self._avoid = {h: t for h, t in self._avoid.items() if t > now}
        return set(self._avoid)

    async def _on_hello(self, session: WsSession, msg: dict) -> None:
        session.hello = True
        new = self._update_candidates(msg)
        requested, self._requested = self._requested, None
        auto_new = self.pcfg.auto_switch_new and not self.is_busy(self.device_id)
        host, why = choose_host(self.registry, self.cands, requested=requested, new_hosts=new,
                                auto_switch_new=auto_new, avoid=self._avoided())
        log.info("[%s] hello on '%s' (gw %s): candidates %s -> %s (%s)", self.device_id, self.cands.ssid,
                 self.cands.gateway, self.cands.hosts, host, why)
        if host is None:
            await self._send(session, {"type": "idle", "reason": "no_phone"})
            return
        if why == "new phone appeared":
            self.registry.pin(None)
        session.target = host
        await self._send(session, {"type": "connect", "host": host, "reason": why})

    async def _on_candidates(self, session: WsSession, msg: dict) -> None:
        before = list(self.cands.hosts)
        new = self._update_candidates(msg)
        if self.cands.hosts != before:
            log.info("[%s] candidates %s -> %s (new: %s)", self.device_id, before, self.cands.hosts, new)
            await self._notify()
        if not new or not self.pcfg.auto_switch_new:
            return
        fresh = [h for h in new if self.registry.by_host(h, self.cands.ssid) is None and h != self.current_host]
        if not fresh:
            return
        if self.is_busy(self.device_id):
            log.info("[%s] new phone at %s, but a task is running: staying on %s", self.device_id, fresh[0],
                     self.current_host)
            return
        log.info("[%s] a new phone appeared at %s, switching to it", self.device_id, fresh[0])
        self.registry.pin(None)
        await self.switch_to(fresh[0], "new phone appeared")

    async def _send(self, session: WsSession | None, obj: dict) -> None:
        if session is None or session.ws.closed:
            return
        with contextlib.suppress(ConnectionError, RuntimeError):
            await session.ws.send_str(json.dumps(obj, ensure_ascii=False))

    # ---- switching (voice commands / API)
    @property
    def multi_phone(self) -> bool:
        """True when the device runs firmware that reports candidates and can be told to switch."""
        return bool(self.cands.updated)

    async def switch_to(self, host: str | None, reason: str) -> bool:
        """Make the device reconnect to `host` (None = let the policy choose again)."""
        if host is not None and host not in self.cands.hosts:
            return False
        log.info("[%s] switching phone %s -> %s (%s)", self.device_id, self.current_host or "-", host or "auto",
                 reason)
        self._requested = host
        if host:
            self._avoid.pop(host, None)
        self.phone = None
        self.current_host = ""
        self.phone_generation += 1
        s = self._session
        if s is not None:
            s.end_code = CLOSE_SWITCH
            await self._send(s, {"type": "switch"})  # in case the close code gets lost on the way
        # Forget the old transport first (adb would otherwise keep the old phone's "device"
        # state for a while); this also ends the paired session, with code 4005.
        with contextlib.suppress(AdbError):
            await self.adb.disconnect(self.serial)
        await self._set_adb_state("absent")
        if s is not None:
            await self._close_session(s, CLOSE_SWITCH, "switch phone")
        await self._notify()
        return True

    async def next_phone(self) -> str | None:
        host = next_host(self.cands, self.current_host)
        if host is not None:
            await self.switch_to(host, "next phone")
        return host

    async def rescan(self, wait_s: float) -> bool:
        """Ask the device to scan its Wi-Fi again; True if a fresh report arrived in time."""
        s = self._session
        if s is None or not s.hello:
            return False
        self._cands_changed.clear()
        await self._send(s, {"type": "rescan"})
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._cands_changed.wait(), wait_s)
            return True
        return False

    async def _identify(self, session_id: int) -> None:
        try:
            res = await self.adb.shell(self.serial, IDENTIFY_CMD, timeout_s=10)
        except AdbError as e:
            log.info("[%s] cannot identify phone: %s", self.device_id, e)
            return
        ident = parse_identity(res.text)
        if ident is None:
            log.info("[%s] phone did not report a serial: %r", self.device_id, res.text[:120])
            return
        self._identified_session = session_id
        prev = self.phone.serial if self.phone else None
        self.phone = self.registry.seen(ident, self.current_host, self.cands.ssid)
        if prev != self.phone.serial:
            self.phone_generation += 1
        log.info("[%s] phone at %s is %s (%s)", self.device_id, self.current_host or "?", self.phone.name,
                 ident.serial)
        await self._send(self._session, {"type": "phone_info", "name": self.phone.name, "serial": ident.serial,
                                         "host": self.current_host})
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
            await self._close_session(session, session.end_code, "adb closed")
            with contextlib.suppress(Exception):
                writer.close()

    # ------------------------------------------------------------------ supervisor
    async def _supervise(self) -> None:
        """Keep `adb connect` alive while a device is attached; reset wedged transports."""
        while True:
            try:
                state = await self.adb.get_state(self.serial)
                await self._set_adb_state(state)
                s = self._session
                if state == "device" and s is not None and s.used and self._identified_session != s.id:
                    self._identified_session = s.id  # one attempt per session
                    await self._identify(s.id)
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
                        if state == "absent":  # (paired + absent: adb forgot it, e.g. after a switch)
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

    def __init__(self, adb: AdbCli, cfg: AdbConfig, on_state_change: StateCallback | None = None,
                 registry: PhoneRegistry | None = None, phones_cfg: PhonesConfig | None = None,
                 is_busy: BusyCallback | None = None):
        self.adb = adb
        self.cfg = cfg
        self.on_state_change = on_state_change
        self.registry = registry or PhoneRegistry()
        self.phones_cfg = phones_cfg or PhonesConfig()
        self.is_busy = is_busy
        self.tunnels: dict[str, AdbTunnel] = {}
        self.last_active: str | None = None

    def get_or_create(self, device_id: str) -> AdbTunnel:
        t = self.tunnels.get(device_id)
        if t is None:
            used = {x.port for x in self.tunnels.values()}
            port = self.cfg.local_port
            while port in used:
                port += 1
            t = AdbTunnel(device_id, port, self.adb, self.cfg, self.on_state_change, self.registry,
                          self.phones_cfg, self.is_busy)
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
