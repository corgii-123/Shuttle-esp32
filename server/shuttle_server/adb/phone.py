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
import struct
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
ADB_IME = "com.android.adbkeyboard/.AdbIME"


class PhoneUnavailable(RuntimeError):
    """No usable phone (no tunnel / fallback serial, or adb state is not "device")."""

    def __init__(self, message: str, state: str = "absent"):
        super().__init__(message)
        self.state = state


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


def _decode_image(data: bytes) -> Image.Image:
    """Open and fully decode (a truncated PNG fails here instead of later)."""
    img = Image.open(io.BytesIO(data))
    img.load()
    return img


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
    def __init__(self, adb: AdbCli, serial: str | Callable[[], str | None], cfg: PhoneConfig,
                 generation: Callable[[], int] | None = None):
        self.adb = adb
        self._serial = serial
        self.cfg = cfg
        # Changes whenever another phone may now sit behind the same serial (127.0.0.1:15555
        # after a phone switch): per-phone caches are dropped.
        self._generation = generation
        self._seen_generation = generation() if generation else 0
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
        if self._generation is not None:
            gen = self._generation()
            if gen != self._seen_generation:
                self._seen_generation = gen
                self.reset()
        if not s:
            raise PhoneUnavailable("手机未连接（ESP32 ADB 隧道未建立，且未配置 adb.fallback_serial）")
        return s

    def reset(self) -> None:
        """Forget everything learned about the phone (size, helper pushed, last screenshot)."""
        self.last = None
        self._helper_pushed.clear()
        self._helper_broken_until = 0.0
        self._device_size.clear()

    async def is_ready(self) -> bool:
        try:
            return await self.adb.get_state(self.serial) == "device"
        except (PhoneUnavailable, AdbError):
            return False

    async def ensure_ready(self) -> None:
        """Raise PhoneUnavailable (with a reason the user can act on) unless adb says "device"."""
        serial = self.serial
        try:
            state = await self.adb.get_state(serial)
        except AdbError as e:
            raise PhoneUnavailable(f"adb 不可用: {e}") from e
        if state == "device":
            return
        reasons = {
            "unauthorized": "手机未授权 USB 调试（请在手机上点“允许”）",
            "offline": "手机 adb 离线",
            "absent": "手机未连接",
        }
        raise PhoneUnavailable(f"{reasons.get(state, '手机状态 ' + state)} ({serial})", state)

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
        error = ""
        for attempt in range(2):
            if attempt:
                # Empty or cut-off PNG: seen on MIUI while the keyguard (secure window) is coming up
                # and while a dozing phone wakes up. One retry.
                await asyncio.sleep(0.7)
            png = await self.adb.exec_out(self.serial, "screencap -p", timeout_s=30)
            if not png:
                error = "截屏失败：手机可能锁屏或正在显示安全页面"
                continue
            if not png.startswith(b"\x89PNG"):
                raise PhoneError(f"screencap returned no PNG ({len(png)} bytes): {png[:80]!r}")
            try:
                img = await asyncio.to_thread(_decode_image, png)
            except (OSError, SyntaxError, ValueError, EOFError, struct.error) as e:
                log.warning("screencap PNG unreadable (%d bytes): %s", len(png), e)
                error = f"截屏失败：图像不完整（{len(png) // 1024} KB）"
                continue
            dw, dh = img.size
            jpeg, w, h = await asyncio.to_thread(
                compress_screenshot, img, self.cfg.screenshot_short_side, self.cfg.screenshot_max_bytes,
                self.cfg.jpeg_quality, self.cfg.jpeg_min_quality)
            return Screenshot(jpeg, w, h, dw, dh, "mac", int((time.monotonic() - t0) * 1000))
        raise PhoneError(error)

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
                log.info("typed %d chars via input_text", len(text))
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
                    log.info("typed %d chars via clipboard + paste", len(text))
                    return "clipboard"
                except (PhoneError, AdbError) as e:
                    errors.append(f"clipboard: {e}")
                    if method == "clipboard":
                        raise PhoneError("; ".join(errors)) from e
            if method in ("auto", "adbkeyboard"):
                pkgs = await self.shell("pm list packages com.android.adbkeyboard", check=False)
                if "com.android.adbkeyboard" in pkgs:
                    # ADBKeyboard only receives the broadcast while it is the active IME:
                    # switch to it for this input, then restore the user's keyboard.
                    previous = (await self.shell("settings get secure default_input_method", check=False)).strip()
                    if previous != ADB_IME:
                        await self.shell(f"ime enable {ADB_IME}", check=False)
                        await self.shell(f"ime set {ADB_IME}", check=False)
                        await asyncio.sleep(0.6)
                    b64 = base64.b64encode(text.encode("utf-8")).decode()
                    await self.shell(f"am broadcast -a ADB_INPUT_B64 --es msg {b64}")
                    log.info("typed %d chars via ADBKeyboard", len(text))
                    if previous and previous != ADB_IME and previous != "null":
                        await asyncio.sleep(0.3)
                        await self.shell(f"ime set {shlex.quote(previous)}", check=False)
                    return "adbkeyboard"
                errors.append("adbkeyboard: not installed")
            raise PhoneError("无法输入中文: " + "; ".join(errors))

    async def wake_and_unlock(self) -> bool:
        """Turn the screen on and dismiss a swipe-only keyguard. False = still locked (PIN etc.)."""
        power = await self.shell("dumpsys power | grep mWakefulness=", check=False)
        if "Awake" not in power:
            log.info("phone screen is off (%s), waking it", power.strip())
            await self.keyevent(224)  # KEYCODE_WAKEUP
            await asyncio.sleep(0.5)
        if not await self._keyguard_showing():
            return True
        async with self._lock:
            await self.shell("wm dismiss-keyguard", check=False)
        await asyncio.sleep(0.8)
        locked = await self._keyguard_showing()
        if locked:
            log.info("keyguard still showing after dismiss-keyguard (secure lock?)")
        return not locked

    async def _keyguard_showing(self) -> bool:
        out = await self.shell("dumpsys window | grep -E 'mDreamingLockscreen=|mShowingLockscreen=|isKeyguardShowing'",
                               check=False)
        return bool(re.search(r"(mDreamingLockscreen|mShowingLockscreen|isKeyguardShowing)=true", out))

    async def launch_app(self, package: str) -> None:
        pkg = shlex.quote(package)
        async with self._lock:
            out = await self.shell(f"monkey -p {pkg} -c android.intent.category.LAUNCHER 1", check=False)
            if "Events injected" in out:
                return
            # monkey can be blocked (e.g. MIUI without "USB debugging (Security settings)"):
            # resolve the launcher activity and start it with am instead.
            log.info("monkey could not launch %s (%s), trying am start", package, out.strip()[-120:])
            resolved = await self.shell(
                f"cmd package resolve-activity --brief -c android.intent.category.LAUNCHER {pkg}", check=False)
            component = resolved.strip().splitlines()[-1].strip() if resolved.strip() else ""
            if "/" not in component:
                raise PhoneError(f"无法启动 {package}: {out.strip()[-120:]}")
            res = await self.shell(f"am start -n {shlex.quote(component)}", check=False)
            if "Error" in res:
                raise PhoneError(f"无法启动 {package}: {res.strip()[-120:]}")

    async def installed_packages(self) -> set[str]:
        out = await self.shell("pm list packages", timeout_s=20)
        return {line[8:].strip() for line in out.splitlines() if line.startswith("package:")}

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
