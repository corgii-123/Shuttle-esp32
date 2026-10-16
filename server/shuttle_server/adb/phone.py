"""PhoneController: everything an agent needs to see and drive the Android phone.

All coordinates given to / returned from this class are in *screenshot space* (the scaled
JPEG returned by screenshot()); they are converted to device pixels internally.
"""
from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import shlex
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PIL import Image

from ..config import PhoneConfig
from .adb_cli import AdbCli, AdbError

log = logging.getLogger(__name__)

HELPER_REMOTE = "/data/local/tmp/shuttle-helper.jar"
HELPER_MAIN = "com.shuttle.helper.Main"
DEFAULT_HELPER = Path(__file__).resolve().parent.parent / "phone_helper" / "shuttle-helper.jar"
HELPER_RETRY_AFTER_S = 600.0

KEY_BACK, KEY_HOME, KEY_ENTER, KEY_APP_SWITCH, KEY_PASTE = 4, 3, 66, 187, 279


class PhoneUnavailable(RuntimeError):
    """No phone reachable (no tunnel up and no adb.fallback_serial)."""


class PhoneError(RuntimeError):
    pass


@dataclass
class Screenshot:
    jpeg: bytes
    width: int  # JPEG size (screenshot space)
    height: int
    device_width: int  # device pixels in the current orientation
    device_height: int
    source: str  # "mac" | "phone"
    elapsed_ms: int

    @property
    def scale(self) -> float:
        return self.width / self.device_width if self.device_width else 1.0


@dataclass
class UiElement:
    text: str
    desc: str
    resource_id: str
    cls: str
    clickable: bool
    bounds: tuple[int, int, int, int]  # screenshot space (x1, y1, x2, y2)

    @property
    def center(self) -> tuple[int, int]:
        x1, y1, x2, y2 = self.bounds
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def label(self) -> str:
        return self.text or self.desc


def compress_screenshot(png_or_image: bytes | Image.Image, short_side: int, max_bytes: int,
                        quality: int = 80, min_quality: int = 35) -> tuple[bytes, int, int]:
    """Scale so the short side is <= short_side, then JPEG-encode under max_bytes."""
    img = png_or_image if isinstance(png_or_image, Image.Image) else Image.open(io.BytesIO(png_or_image))
    img = img.convert("RGB")
    w, h = img.size
    s = min(1.0, short_side / min(w, h))
    if s < 1.0:
        img = img.resize((max(1, round(w * s)), max(1, round(h * s))), Image.LANCZOS)
    while True:
        q = quality
        while True:
            buf = io.BytesIO()
            img.save(buf, "JPEG", quality=q, optimize=True)
            data = buf.getvalue()
            if len(data) <= max_bytes or q <= min_quality:
                break
            q = max(min_quality, q - 10)
        if len(data) <= max_bytes or min(img.size) <= 160:
            return data, img.size[0], img.size[1]
        img = img.resize((max(1, int(img.size[0] * 0.85)), max(1, int(img.size[1] * 0.85))), Image.LANCZOS)


def escape_input_text(text: str) -> str:
    """Escape for `adb shell input text` (ASCII only): spaces -> %s, shell-quote the rest."""
    return shlex.quote(text.replace("%", "\\%").replace(" ", "%s"))


class PhoneController:
    def __init__(self, adb: AdbCli, serial: str | Callable[[], str | None], cfg: PhoneConfig):
        self.adb = adb
        self._serial = serial
        self.cfg = cfg
        self.helper_jar = Path(cfg.helper_jar).expanduser() if cfg.helper_jar else DEFAULT_HELPER
        self.last: Screenshot | None = None
        self._helper_pushed: set[str] = set()
        self._helper_broken_until = 0.0
        self._device_size: dict[str, tuple[int, int]] = {}
        self._lock = asyncio.Lock()  # one adb action at a time keeps the phone state coherent

    # ------------------------------------------------------------------ plumbing
    @property
    def serial(self) -> str:
        s = self._serial() if callable(self._serial) else self._serial
        if not s:
            raise PhoneUnavailable("手机未连接（ESP32 ADB 隧道未建立，且未配置 adb.fallback_serial）")
        return s

    async def is_ready(self) -> bool:
        try:
            return await self.adb.get_state(self.serial) == "device"
        except (PhoneUnavailable, AdbError):
            return False

    async def shell(self, command: str, timeout_s: float | None = None, check: bool = True) -> str:
        res = await self.adb.shell(self.serial, command, timeout_s=timeout_s)
        if check:
            res.check(f"shell {command[:40]}")
        return res.text

    async def device_size(self) -> tuple[int, int]:
        """Physical size from `wm size` (portrait). Prefer the last screenshot for orientation."""
        serial = self.serial
        if serial not in self._device_size:
            out = await self.shell("wm size")
            m = re.findall(r"(\d+)x(\d+)", out)
            if not m:
                raise PhoneError(f"cannot parse wm size: {out!r}")
            w, h = map(int, m[-1])  # "Override size" comes last when present
            self._device_size[serial] = (w, h)
        return self._device_size[serial]

    # ------------------------------------------------------------------ coordinates
    def to_device(self, x: float, y: float) -> tuple[int, int]:
        if self.last is None:
            return int(round(x)), int(round(y))
        s = self.last.scale
        dx = int(round(x / s))
        dy = int(round(y / s))
        return (min(max(dx, 0), self.last.device_width - 1), min(max(dy, 0), self.last.device_height - 1))

    def to_screen(self, x: float, y: float) -> tuple[int, int]:
        if self.last is None:
            return int(round(x)), int(round(y))
        s = self.last.scale
        return int(round(x * s)), int(round(y * s))

    # ------------------------------------------------------------------ screenshot
    async def screenshot(self) -> bytes:
        """JPEG <= phone.screenshot_max_bytes, short side ~phone.screenshot_short_side."""
        return (await self.screenshot_info()).jpeg

    async def screenshot_info(self) -> Screenshot:
        async with self._lock:
            shot = None
            if self.cfg.device_side_screenshot and time.monotonic() >= self._helper_broken_until:
                try:
                    shot = await self._screenshot_on_phone()
                except (PhoneError, AdbError, OSError) as e:
                    log.warning("phone-side screenshot failed, falling back to Mac compression: %s", e)
                    self._helper_broken_until = time.monotonic() + HELPER_RETRY_AFTER_S
            if shot is None:
                shot = await self._screenshot_on_mac()
            self.last = shot
            log.info("screenshot %dx%d (device %dx%d) %d KB via %s in %d ms", shot.width, shot.height,
                     shot.device_width, shot.device_height, len(shot.jpeg) // 1024, shot.source, shot.elapsed_ms)
            return shot

    async def _screenshot_on_mac(self) -> Screenshot:
        t0 = time.monotonic()
        png = await self.adb.exec_out(self.serial, "screencap -p", timeout_s=30)
        if not png.startswith(b"\x89PNG"):
            raise PhoneError(f"screencap returned no PNG ({len(png)} bytes): {png[:80]!r}")
        img = Image.open(io.BytesIO(png))
        dw, dh = img.size
        jpeg, w, h = await asyncio.to_thread(
            compress_screenshot, img, self.cfg.screenshot_short_side, self.cfg.screenshot_max_bytes,
            self.cfg.jpeg_quality, self.cfg.jpeg_min_quality)
        return Screenshot(jpeg, w, h, dw, dh, "mac", int((time.monotonic() - t0) * 1000))

    async def _ensure_helper(self) -> None:
        serial = self.serial
        if serial in self._helper_pushed:
            return
        if not self.helper_jar.exists():
            raise PhoneError(f"helper jar not found: {self.helper_jar} (see server/phone_helper/README.md)")
        local_size = self.helper_jar.stat().st_size
        remote = (await self.shell(f"stat -c %s {HELPER_REMOTE} 2>/dev/null", check=False)).strip()
        if remote != str(local_size):
            log.info("pushing %s -> %s", self.helper_jar.name, HELPER_REMOTE)
            await self.adb.push(serial, str(self.helper_jar), HELPER_REMOTE)
        self._helper_pushed.add(serial)

    def _helper_cmd(self, *args: str) -> str:
        return f"CLASSPATH={HELPER_REMOTE} app_process /system/bin {HELPER_MAIN} " + " ".join(
            shlex.quote(a) for a in args)

    async def _screenshot_on_phone(self) -> Screenshot:
        t0 = time.monotonic()
        await self._ensure_helper()
        out = await self.adb.exec_out(self.serial, self._helper_cmd(
            "screenshot", str(self.cfg.screenshot_short_side), str(self.cfg.jpeg_quality),
            str(self.cfg.screenshot_max_bytes)), timeout_s=30)
        # Helper output: b"SHUTTLE1 <devW> <devH>\n" + JPEG bytes
        nl = out.find(b"\n")
        header = out[:nl].decode(errors="replace") if nl > 0 else ""
        m = re.match(r"SHUTTLE1 (\d+) (\d+)$", header)
        jpeg = out[nl + 1:] if m else b""
        if not m or not jpeg.startswith(b"\xff\xd8"):
            raise PhoneError(f"unexpected helper output: {out[:120]!r}")
        with Image.open(io.BytesIO(jpeg)) as img:
            w, h = img.size
        if len(jpeg) > self.cfg.screenshot_max_bytes:  # helper should not do this, but be safe
            jpeg, w, h = await asyncio.to_thread(compress_screenshot, jpeg, self.cfg.screenshot_short_side,
                                                 self.cfg.screenshot_max_bytes, self.cfg.jpeg_quality,
                                                 self.cfg.jpeg_min_quality)
        return Screenshot(jpeg, w, h, int(m.group(1)), int(m.group(2)), "phone",
                          int((time.monotonic() - t0) * 1000))

    # ------------------------------------------------------------------ input
    async def tap(self, x: float, y: float) -> None:
        dx, dy = self.to_device(x, y)
        log.info("tap (%d,%d) -> device (%d,%d)", x, y, dx, dy)
        async with self._lock:
            await self.shell(f"input tap {dx} {dy}")

    async def long_press(self, x: float, y: float, duration_ms: int = 800) -> None:
        dx, dy = self.to_device(x, y)
        async with self._lock:
            await self.shell(f"input swipe {dx} {dy} {dx} {dy} {int(duration_ms)}")

    async def swipe(self, x1: float, y1: float, x2: float, y2: float, duration_ms: int = 300) -> None:
        a = self.to_device(x1, y1)
        b = self.to_device(x2, y2)
        log.info("swipe %s -> %s (%d ms)", a, b, duration_ms)
        async with self._lock:
            await self.shell(f"input swipe {a[0]} {a[1]} {b[0]} {b[1]} {int(duration_ms)}")

    async def scroll(self, direction: str = "down") -> None:
        """Scroll the content (finger moves opposite). Uses the last screenshot size."""
        w = self.last.width if self.last else 720
        h = self.last.height if self.last else 1280
        cx = w // 2
        if direction == "down":
            await self.swipe(cx, h * 0.75, cx, h * 0.30)
        elif direction == "up":
            await self.swipe(cx, h * 0.30, cx, h * 0.75)
        elif direction == "left":
            await self.swipe(w * 0.8, h // 2, w * 0.2, h // 2)
        else:
            await self.swipe(w * 0.2, h // 2, w * 0.8, h // 2)

    async def keyevent(self, code: int | str) -> None:
        async with self._lock:
            await self.shell(f"input keyevent {code}")

    async def back(self) -> None:
        await self.keyevent(KEY_BACK)

    async def home(self) -> None:
        await self.keyevent(KEY_HOME)

    async def recent_apps(self) -> None:
        await self.keyevent(KEY_APP_SWITCH)

    async def enter(self) -> None:
        await self.keyevent(KEY_ENTER)

    async def input_text(self, text: str) -> str:
        """Type text into the focused field. Chinese/emoji need clipboard (helper) or ADBKeyboard.

        Returns the method used.
        """
        if not text:
            return "none"
        method = self.cfg.input_method
        ascii_ok = all(32 <= ord(c) < 127 for c in text)
        async with self._lock:
            if method == "input_text" or (method == "auto" and ascii_ok):
                if not ascii_ok:
                    raise PhoneError("input_method=input_text 不支持非 ASCII 字符")
                await self.shell(f"input text {escape_input_text(text)}")
                return "input_text"
            errors = []
            if method in ("auto", "clipboard"):
                try:
                    await self._ensure_helper()
                    b64 = base64.b64encode(text.encode("utf-8")).decode()
                    out = await self.shell(self._helper_cmd("clip", b64), timeout_s=15)
                    if "OK" not in out:
                        raise PhoneError(f"helper clip failed: {out.strip()[:200]}")
                    await self.shell(f"input keyevent {KEY_PASTE}")
                    return "clipboard"
                except (PhoneError, AdbError) as e:
                    errors.append(f"clipboard: {e}")
                    if method == "clipboard":
                        raise PhoneError("; ".join(errors)) from e
            if method in ("auto", "adbkeyboard"):
                pkgs = await self.shell("pm list packages com.android.adbkeyboard", check=False)
                if "com.android.adbkeyboard" in pkgs:
                    b64 = base64.b64encode(text.encode("utf-8")).decode()
                    await self.shell(f"am broadcast -a ADB_INPUT_B64 --es msg {b64}")
                    return "adbkeyboard"
                errors.append("adbkeyboard: not installed")
            raise PhoneError("无法输入中文: " + "; ".join(errors))

    async def launch_app(self, package: str) -> None:
        async with self._lock:
            await self.shell(f"monkey -p {shlex.quote(package)} -c android.intent.category.LAUNCHER 1")

    async def current_app(self) -> str:
        out = await self.shell("dumpsys window | grep -E 'mCurrentFocus|mFocusedApp'", check=False)
        m = re.search(r"([a-zA-Z0-9_.]+)/[a-zA-Z0-9_.$]+", out)
        return m.group(1) if m else ""

    # ------------------------------------------------------------------ UI tree
    async def dump_ui(self) -> str:
        """uiautomator XML of the current screen (device coordinates)."""
        remote = "/data/local/tmp/shuttle_ui.xml"
        async with self._lock:
            last_err = ""
            for _ in range(2):
                out = await self.shell(f"uiautomator dump {remote}", timeout_s=self.cfg.ui_dump_timeout_s,
                                       check=False)
                if "dumped to" in out:
                    xml = await self.shell(f"cat {remote}")
                    return xml[xml.find("<"):]
                last_err = out.strip()
                await asyncio.sleep(0.5)
            raise PhoneError(f"uiautomator dump failed: {last_err[:200]}")

    async def ui_elements(self, only_meaningful: bool = True) -> list[UiElement]:
        xml = await self.dump_ui()
        return self.parse_ui(xml, only_meaningful)

    def parse_ui(self, xml: str, only_meaningful: bool = True) -> list[UiElement]:
        try:
            root = ET.fromstring(xml)
        except ET.ParseError as e:
            raise PhoneError(f"bad UI xml: {e}") from e
        result = []
        for node in root.iter("node"):
            text = node.get("text", "")
            desc = node.get("content-desc", "")
            clickable = node.get("clickable") == "true"
            if only_meaningful and not (text or desc or clickable):
                continue
            m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", node.get("bounds", ""))
            if not m:
                continue
            x1, y1, x2, y2 = map(int, m.groups())
            a = self.to_screen(x1, y1)
            b = self.to_screen(x2, y2)
            result.append(UiElement(text, desc, node.get("resource-id", ""), node.get("class", ""), clickable,
                                    (a[0], a[1], b[0], b[1])))
        return result

    async def find_element(self, label: str) -> UiElement | None:
        """Best match by visible text / content-desc (exact first, then substring)."""
        elems = await self.ui_elements()
        for e in elems:
            if e.label == label:
                return e
        for e in elems:
            if label in e.label:
                return e
        return None
