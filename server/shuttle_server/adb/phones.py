"""Which phones exist, and which one the device should talk to.

The ESP32 finds phones by itself (it scans its Wi-Fi for adbd on :5555) and reports the
candidate IPs in its /ws/adb hello. The Mac recognises each phone once adb has talked to it
(ro.serialno + model) and remembers it here, so the choice can be made by *phone*, not by IP:

  1. a switch the user just asked for ("换手机", "切换到小米", POST /api/phones/select)
  2. a phone that just appeared and was never seen before (someone just ran `adb tcpip` on it:
     the freshest sign of what the user wants), when phones.auto_switch_new is on and no task
     is running; this clears the pin
  3. the pinned phone (last explicit choice), if it is among the candidates
  4. the most recently used known phone among the candidates
  5. the hotspot phone (the gateway), then the device's own last choice, then the first one

State is a small JSON file (phones.state_file) so names and last-used survive restarts.
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

log = logging.getLogger(__name__)

# One shell call, one line per value (empty line when a property does not exist).
IDENTIFY_CMD = ("getprop ro.serialno; getprop ro.boot.serialno; settings get secure android_id; "
                "getprop ro.product.manufacturer; getprop ro.product.model; settings get global device_name")


@dataclass
class PhoneIdentity:
    serial: str
    manufacturer: str = ""
    model: str = ""
    device_name: str = ""


def parse_identity(text: str) -> PhoneIdentity | None:
    lines = [l.strip() for l in text.replace("\r", "").split("\n")]
    lines += [""] * (6 - len(lines))

    def clean(v: str) -> str:
        return "" if v in ("null", "unknown") else v

    serial = next((clean(v) for v in lines[:3] if clean(v)), "")
    if not serial:
        return None
    return PhoneIdentity(serial=serial, manufacturer=clean(lines[3]), model=clean(lines[4]),
                         device_name=clean(lines[5]))


@dataclass
class PhoneRecord:
    serial: str
    manufacturer: str = ""
    model: str = ""
    device_name: str = ""
    alias: str = ""
    last_host: str = ""
    last_ssid: str = ""
    first_seen: float = 0.0
    last_seen: float = 0.0
    last_used: float = 0.0

    @property
    def name(self) -> str:
        if self.alias:
            return self.alias
        if self.device_name:
            return self.device_name
        model = self.model
        if self.manufacturer and not model.lower().startswith(self.manufacturer.lower()):
            model = f"{self.manufacturer} {model}".strip()
        return model or self.serial

    def names(self) -> list[str]:
        """Everything the user might call this phone (lower-case, spaces removed)."""
        out = [self.alias, self.device_name, self.model, self.manufacturer, self.serial, self.name]
        return [norm(n) for n in out if n]

    def public(self) -> dict:
        d = asdict(self)
        d["name"] = self.name
        return d


def norm(s: str) -> str:
    return re.sub(r"[\s，。,.!！？?、:：\"'“”]", "", s).lower()


@dataclass
class Candidates:
    """What one device last reported: the adbd hosts it can reach on its current Wi-Fi."""
    ssid: str = ""
    gateway: str = ""
    ip: str = ""
    hosts: list[str] = field(default_factory=list)
    last: str = ""
    fixed: bool = False
    updated: float = 0.0


class PhoneRegistry:
    def __init__(self, path: str = "", aliases: dict[str, str] | None = None):
        self.path = Path(path).expanduser() if path else None
        self.aliases = dict(aliases or {})
        self.phones: dict[str, PhoneRecord] = {}
        self.pinned: str | None = None  # serial of the user's explicit choice
        self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            names = {f.name for f in fields(PhoneRecord)}
            for raw in data.get("phones", []):
                rec = PhoneRecord(**{k: v for k, v in raw.items() if k in names})
                self.phones[rec.serial] = rec
            self.pinned = data.get("pinned") or None
        except (OSError, ValueError, TypeError) as e:
            log.warning("cannot read %s (%s), starting with an empty phone list", self.path, e)
        self._apply_aliases()
        log.info("phone registry: %d known phone(s) from %s", len(self.phones), self.path)

    def save(self) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"phones": [asdict(p) for p in self.phones.values()], "pinned": self.pinned},
                                      ensure_ascii=False, indent=1), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as e:
            log.warning("cannot write %s: %s", self.path, e)

    def _apply_aliases(self) -> None:
        for serial, alias in self.aliases.items():
            if serial in self.phones:
                self.phones[serial].alias = alias

    # ------------------------------------------------------------------ lookups
    def get(self, serial: str | None) -> PhoneRecord | None:
        return self.phones.get(serial) if serial else None

    def by_host(self, host: str, ssid: str = "") -> PhoneRecord | None:
        """The phone last seen at this IP (on this Wi-Fi, when the SSID is known)."""
        best = None
        for p in self.phones.values():
            if p.last_host == host and (not ssid or not p.last_ssid or p.last_ssid == ssid):
                if best is None or p.last_seen > best.last_seen:
                    best = p
        return best

    def find_by_name(self, text: str) -> PhoneRecord | None:
        """Phone whose alias/name/model appears in `text` (longest match wins)."""
        t = norm(text)
        best, best_len = None, 0
        for p in self.phones.values():
            for n in p.names():
                if len(n) >= 2 and n in t and len(n) > best_len:
                    best, best_len = p, len(n)
        return best

    def all(self) -> list[PhoneRecord]:
        return sorted(self.phones.values(), key=lambda p: p.last_used, reverse=True)

    # ------------------------------------------------------------------ updates
    def seen(self, ident: PhoneIdentity, host: str, ssid: str, used: bool = True) -> PhoneRecord:
        now = time.time()
        rec = self.phones.get(ident.serial)
        if rec is None:
            rec = PhoneRecord(serial=ident.serial, first_seen=now)
            self.phones[ident.serial] = rec
            log.info("new phone %s (%s %s) at %s", ident.serial, ident.manufacturer, ident.model, host)
        rec.manufacturer = ident.manufacturer or rec.manufacturer
        rec.model = ident.model or rec.model
        rec.device_name = ident.device_name or rec.device_name
        rec.alias = self.aliases.get(rec.serial, rec.alias)
        if host:
            # an IP belongs to one phone at a time (DHCP may have handed it over)
            for other in self.phones.values():
                if other is not rec and other.last_host == host and other.last_ssid == ssid:
                    other.last_host = ""
            rec.last_host = host
            rec.last_ssid = ssid
        rec.last_seen = now
        if used:
            rec.last_used = now
        self.save()
        return rec

    def pin(self, serial: str | None) -> None:
        self.pinned = serial
        self.save()

    def forget_host(self, host: str) -> None:
        for p in self.phones.values():
            if p.last_host == host:
                p.last_host = ""
        self.save()


def choose_host(reg: PhoneRegistry, cands: Candidates, *, requested: str | None = None,
                new_hosts: list[str] | None = None, auto_switch_new: bool = True,
                avoid: set[str] | None = None) -> tuple[str | None, str]:
    """Pick the host the device should dial. Returns (host, reason)."""
    hosts = [h for h in cands.hosts if h]
    if not hosts:
        return None, "no candidates"
    usable = [h for h in hosts if h not in (avoid or set())] or hosts
    if requested and requested in hosts:
        return requested, "requested"
    if auto_switch_new:
        fresh = [h for h in (new_hosts or []) if h in usable and reg.by_host(h, cands.ssid) is None]
        if fresh:
            return fresh[0], "new phone appeared"
    pinned = reg.get(reg.pinned)
    if pinned and pinned.last_host in usable:
        return pinned.last_host, f"pinned {pinned.name}"
    known = [(p, p.last_host) for p in reg.all() if p.last_host in usable
             and (not cands.ssid or not p.last_ssid or p.last_ssid == cands.ssid)]
    if known:
        p, h = known[0]
        return h, f"last used {p.name}"
    if cands.gateway in usable:
        return cands.gateway, "hotspot phone"
    if cands.last in usable:
        return cands.last, "device's last phone"
    return usable[0], "first found"


def next_host(cands: Candidates, current: str) -> str | None:
    """The candidate after `current` (round robin) - "换手机"."""
    hosts = [h for h in cands.hosts if h]
    others = [h for h in hosts if h != current]
    if not others:
        return None
    if current in hosts:
        i = hosts.index(current)
        return hosts[(i + 1) % len(hosts)] if hosts[(i + 1) % len(hosts)] != current else others[0]
    return others[0]

