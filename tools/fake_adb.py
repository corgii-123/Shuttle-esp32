#!/usr/bin/env python3
"""A fake `adb` executable that simulates one Android phone. For tests and demos.

Point the server at it:  adb.path: /path/to/tools/fake_adb.py   (and adb.fallback_serial: fake-phone)

Env:
  FAKE_ADB_LOG    append every invocation as a JSON line (default: none)
  FAKE_ADB_STATE  file remembering `adb connect`ed serials (default: /tmp/fake_adb_state.json)
  FAKE_ADB_SERIAL serial that is always "device" (default: fake-phone)
  FAKE_ADB_STATE_OF  state reported for connected serials (default: device; try unauthorized)
  FAKE_ADB_SERIALNO / FAKE_ADB_MODEL  identity answered to getprop (default FAKE0001 / Fake Phone)
"""
from __future__ import annotations

import io
import json
import os
import sys
import time

W, H = 1080, 2400
UI_XML = """<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation="0">
<node index="0" text="" resource-id="" class="android.widget.FrameLayout" package="com.android.launcher3" content-desc="" clickable="false" bounds="[0,0][1080,2400]">
<node index="1" text="设置" resource-id="com.android.launcher3:id/icon" class="android.widget.TextView" package="com.android.launcher3" content-desc="设置" clickable="true" bounds="[60,300][300,560]" />
<node index="2" text="微信" resource-id="com.android.launcher3:id/icon" class="android.widget.TextView" package="com.android.launcher3" content-desc="微信" clickable="true" bounds="[320,300][560,560]" />
<node index="3" text="相册" resource-id="com.android.launcher3:id/icon" class="android.widget.TextView" package="com.android.launcher3" content-desc="相册" clickable="true" bounds="[580,300][820,560]" />
<node index="4" text="搜索" resource-id="com.android.launcher3:id/search" class="android.widget.EditText" package="com.android.launcher3" content-desc="" clickable="true" bounds="[60,2100][1020,2250]" />
</node></hierarchy>"""


def log_call(argv: list[str]) -> None:
    path = os.environ.get("FAKE_ADB_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "argv": argv}, ensure_ascii=False) + "\n")


def state_path() -> str:
    return os.environ.get("FAKE_ADB_STATE", "/tmp/fake_adb_state.json")


def load_state() -> list[str]:
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def save_state(serials: list[str]) -> None:
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump(sorted(set(serials)), f)


def screencap_png() -> bytes:
    from PIL import Image, ImageDraw

    img = Image.new("RGB", (W, H), (240, 240, 245))
    d = ImageDraw.Draw(img)
    for x0, label in ((60, "S"), (320, "W"), (580, "P")):
        d.rectangle([x0, 300, x0 + 240, 560], fill=(80, 120, 200))
        d.text((x0 + 100, 400), label, fill=(255, 255, 255))
    d.rectangle([60, 2100, 1020, 2250], outline=(0, 0, 0), width=4)
    # noise so the PNG/JPEG sizes are realistic
    import random
    rnd = random.Random(1)
    for _ in range(4000):
        x, y = rnd.randrange(W), rnd.randrange(H)
        d.point((x, y), fill=(rnd.randrange(256), rnd.randrange(256), rnd.randrange(256)))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def shell(cmd: str) -> tuple[bytes, int]:
    c = cmd.strip()
    if c.startswith("screencap"):
        return screencap_png(), 0
    if c.startswith("wm size"):
        return f"Physical size: {W}x{H}\n".encode(), 0
    if c.startswith("uiautomator dump"):
        path = c.split()[-1] if len(c.split()) > 2 else "/sdcard/window_dump.xml"
        return f"UI hierchary dumped to: {path}\n".encode(), 0
    if c.startswith("cat ") and c.endswith(".xml"):
        return UI_XML.encode(), 0
    if c.startswith("dumpsys window"):
        return b"  mCurrentFocus=Window{1a2b u0 com.android.launcher3/com.android.launcher3.Launcher}\n", 0
    if c.startswith("pm list packages"):
        return b"", 0
    if c.startswith("stat "):
        return b"", 1
    if "getprop ro.serialno" in c:  # phone identification (several `;`-separated queries)
        props = {
            "getprop ro.serialno": os.environ.get("FAKE_ADB_SERIALNO", "FAKE0001"),
            "getprop ro.product.manufacturer": "Shuttle",
            "getprop ro.product.model": os.environ.get("FAKE_ADB_MODEL", "Fake Phone"),
        }
        return "".join(props.get(q.strip(), "") + "\n" for q in c.split(";")).encode(), 0
    if c.startswith(("input ", "monkey ", "am ", "getprop")):
        if c.startswith("monkey"):
            return b"Events injected: 1\n", 0
        return b"", 0
    return f"/system/bin/sh: {c.split()[0]}: inaccessible or not found\n".encode(), 127


def main(argv: list[str]) -> int:
    log_call(argv)
    args = list(argv)
    serial = None
    if len(args) >= 2 and args[0] == "-s":
        serial, args = args[1], args[2:]
    if not args:
        print("fake adb: no command", file=sys.stderr)
        return 1
    cmd, rest = args[0], args[1:]
    default = os.environ.get("FAKE_ADB_SERIAL", "fake-phone")
    if cmd == "version":
        print("Android Debug Bridge version 1.0.41 (fake)")
        return 0
    if cmd in ("start-server", "kill-server"):
        return 0
    if cmd == "devices":
        st = os.environ.get("FAKE_ADB_STATE_OF", "device")
        print("List of devices attached")
        print(f"{default}\tdevice")
        for s in load_state():
            print(f"{s}\t{st}")
        return 0
    if cmd == "connect":
        save_state(load_state() + rest[:1])
        print(f"connected to {rest[0]}")
        return 0
    if cmd == "disconnect":
        save_state([s for s in load_state() if s not in rest])
        print(f"disconnected {rest[0] if rest else 'everything'}")
        return 0
    if cmd == "usb":
        print("restarting in USB mode")
        return 0
    if cmd == "push":
        print(f"{rest[0]}: 1 file pushed. 0.1 MB/s")
        return 0
    if cmd in ("shell", "exec-out"):
        out, rc = shell(" ".join(rest))
        sys.stdout.buffer.write(out)
        return rc
    print(f"fake adb: unsupported command {cmd}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
