"""Phone switching by voice (and the /api/phones endpoints).

Handled before the agent, only when the sentence clearly is about *which phone* is used:
  "换手机" / "换一台手机" / "下一台手机"           -> next phone the device found
  "切换到小米" / "用工作机" / "连接 Pixel"          -> that phone (only if the name is a known phone;
                                                    "切换到飞行模式" still goes to the agent)
  "现在连的是哪台手机" / "有哪些手机"               -> say it
  "关闭这台手机的无线调试" / "断开这台手机"          -> `adb usb` on it (undoes `adb tcpip`), switch away
"""
from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from ..adb.phones import PhoneRecord, norm

if TYPE_CHECKING:
    from ..adb.tunnel import AdbTunnel
    from ..app import ShuttleApp

log = logging.getLogger(__name__)

_PHONE = r"(手机|电话|设备)"
RE_LIST = re.compile(rf"(有|找到|发现|附近)(了)?(哪些|几台|几个|几部|多少台?)(的)?{_PHONE}|附近的?{_PHONE}")
RE_WHICH = re.compile(rf"(哪|那)(一)?(台|个|部){_PHONE}|(连|用|控制)(的|着)(是)?(什么|啥){_PHONE}")
RE_NEXT = re.compile(rf"^(请|帮我|给我)?(换|切换|切|轮换)(一下)?(到)?(一|另一|另外一|下一|别的|其他)?(个|台|部)?{_PHONE}(吧|呀|啊)?$"
                     rf"|^(请)?(下一|另一)(个|台|部){_PHONE}(吧)?$")
RE_SELECT = re.compile(r"^(请|帮我)?(切换到|切换成|切到|换到|换成|改用|改成|使用|用|连接到|连接|连到|连上|连)(.+)$")
RE_DISABLE = re.compile(rf"(关闭|关掉|关了|停用|断开)(这|这个|这台|这部|当前|现在)?(的)?{_PHONE}?(的)?"
                        rf"(无线调试|无线adb|adb|调试|tcpip)"
                        rf"|^(断开|断掉)(这|这个|这台|这部|当前)?(的)?{_PHONE}$")


def _label(t: "AdbTunnel", host: str) -> str:
    rec = t.registry.by_host(host, t.cands.ssid)
    if rec:
        return rec.name
    return "热点手机" if host == t.cands.gateway else "一台新手机"


def _only_names(rest: str, rec: PhoneRecord) -> bool:
    """"切换到小米" yes, "用小米打开微信" no: nothing but the phone's name may be left."""
    for n in sorted(rec.names(), key=len, reverse=True):
        if n:
            rest = rest.replace(n, "")
    return not re.sub(r"(那|这|一)?(台|部|个)?(手机|电话|设备)?(的)?(吧|呀|啊)?", "", rest)


class PhoneCommands:
    def __init__(self, app: "ShuttleApp"):
        self.app = app

    # ------------------------------------------------------------------ voice
    async def handle_voice(self, text: str, device_id: str) -> str | None:
        t = norm(text)
        if not t or len(t) > 30:
            return None
        if RE_DISABLE.search(t):
            return (await self.disable(self._tunnel(device_id)))["message"]
        if RE_LIST.search(t) or RE_WHICH.search(t):
            return self.describe(self._tunnel(device_id))
        if RE_NEXT.search(t):
            return (await self.next(self._tunnel(device_id)))["message"]
        m = RE_SELECT.match(t)
        if m:
            rec = self.app.registry.find_by_name(m.group(3))
            if rec is not None and _only_names(m.group(3), rec):
                return (await self.select(self._tunnel(device_id), serial=rec.serial))["message"]
        return None

    def _tunnel(self, device_id: str) -> "AdbTunnel | None":
        return self.app.tunnels.find(device_id)

    # ------------------------------------------------------------------ actions
    def describe(self, t: "AdbTunnel | None") -> str:
        if t is None or (not t.device_connected and not t.cands.hosts):
            return "板子还没有连上手机通道"
        others = [h for h in t.cands.hosts if h != t.current_host]
        names = "、".join(_label(t, h) for h in others[:4])
        if t.phone and t.current_host:
            msg = f"现在连的是{t.phone.name}"
        elif t.current_host:
            msg = "现在连着一台还没认出来的手机"
        else:
            msg = "现在没有连手机"
        if others:
            msg += f"，附近还有{len(others)}台：{names}"
        elif not t.cands.hosts:
            msg += "，附近也没找到开了无线调试的手机"
        return msg

    async def next(self, t: "AdbTunnel | None") -> dict[str, Any]:
        if t is None or not t.device_connected:
            return {"ok": False, "message": "板子还没有连上手机通道"}
        if not t.multi_phone:
            return {"ok": False, "message": "板子固件不支持切换手机，请升级固件"}
        if len([h for h in t.cands.hosts if h != t.current_host]) == 0:
            await t.rescan(self.app.cfg.phones.rescan_wait_s)
        host = await t.next_phone()
        if host is None:
            return {"ok": False, "message": "附近只找到这一台手机" if t.cands.hosts else "附近没找到手机"}
        self.app.registry.pin(None)  # "last used" decides from now on: the phone we switch to
        return {"ok": True, "host": host, "message": f"好的，换到{_label(t, host)}"}

    async def select(self, t: "AdbTunnel | None", *, host: str | None = None, serial: str | None = None,
                     name: str | None = None, pin: bool = True) -> dict[str, Any]:
        reg = self.app.registry
        rec: PhoneRecord | None = reg.get(serial) if serial else (reg.find_by_name(name) if name else None)
        if (serial or name) and rec is None:
            return {"ok": False, "message": f"不认识{name or serial}这台手机"}
        if rec is not None:
            host = rec.last_host
        if t is None or not t.device_connected:
            if rec is not None and pin:
                reg.pin(rec.serial)
            return {"ok": False, "message": "板子还没有连上手机通道，连上后会用这台手机"}
        label = rec.name if rec else (_label(t, host) if host else "")
        if not host or host not in t.cands.hosts:
            await t.rescan(self.app.cfg.phones.rescan_wait_s)
            if rec is not None:
                host = rec.last_host
            if not host or host not in t.cands.hosts:
                return {"ok": False, "message": f"附近没找到{label or '这台手机'}，确认它连着同一个Wi-Fi并开了无线调试"}
        if pin and rec is not None:
            reg.pin(rec.serial)
        if host == t.current_host and t.phone_link == "connected":
            return {"ok": True, "host": host, "message": f"现在用的就是{label}"}
        await t.switch_to(host, f"user selected {label}")
        return {"ok": True, "host": host, "message": f"好的，切换到{label}"}

    async def disable(self, t: "AdbTunnel | None") -> dict[str, Any]:
        if t is None or not t.current_host:
            return {"ok": False, "message": "现在没有连手机"}
        phone = await t.disable_current()
        name = phone.name if phone else "这台手机"
        if phone is not None and self.app.registry.pinned == phone.serial:
            self.app.registry.pin(None)
        return {"ok": True, "message": f"已关闭{name}的无线调试，再用它要重新执行 adb tcpip 5555"}
