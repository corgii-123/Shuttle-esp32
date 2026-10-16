#!/usr/bin/env python3
"""Build the Shuttle firmware.

Thin wrapper around xiaozhi's canonical scripts/build.py:
  * merges your private settings (server URL, token, Wi-Fi) from shuttle.local.conf
    and/or SHUTTLE_* environment variables into a git-ignored board config
    (main/boards/shuttle/epaper-1.54-v2/config.local.json);
  * builds board "shuttle/epaper-1.54-v2", variant "shuttle-epaper-1.54-v2", zh-CN.

Usage (inside an ESP-IDF v6.1 environment, from firmware/):
    python build_shuttle.py                # uses shuttle.local.conf if present
    python build_shuttle.py --language zh-CN --zip
Outputs: build/merged-binary.bin (flash at 0x0) and the usual build/*.bin files.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
BOARD = "shuttle/epaper-1.54-v2"
VARIANT = "shuttle-epaper-1.54-v2"
BOARD_DIR = ROOT / "main" / "boards" / BOARD
LOCAL_JSON = "config.local.json"

# Environment variable -> Kconfig symbol (string values). Handy for CI secrets.
ENV_STRING_KEYS = {
    "SHUTTLE_SERVER_URL": "CONFIG_SHUTTLE_SERVER_URL",
    "SHUTTLE_TOKEN": "CONFIG_SHUTTLE_TOKEN",
    **{f"SHUTTLE_WIFI{i}_{k}": f"CONFIG_SHUTTLE_WIFI{i}_{k}"
       for i in (1, 2, 3) for k in ("SSID", "PASSWORD", "PHONE_HOST")},
}
LINE_RE = re.compile(r"^(CONFIG_[A-Z0-9_]+)=(.*)$")


def quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def load_local_conf(path: Path) -> list[str]:
    if not path.exists():
        return []
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not LINE_RE.match(line):
            sys.exit(f"[ERROR] {path}: invalid line (expected CONFIG_X=value): {raw}")
        lines.append(line)
    return lines


def merge(base: list[str], extra: list[str]) -> list[str]:
    values: dict[str, str] = {}
    for item in [*base, *extra]:
        values[item.split("=", 1)[0]] = item
    return list(values.values())


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--conf", default=str(ROOT / "shuttle.local.conf"), help="private sdkconfig fragment")
    ap.add_argument("--language", default="zh-CN")
    ap.add_argument("--zip", action="store_true", help="also create releases/*.zip")
    ap.add_argument("--dry-run", action="store_true", help="only write config.local.json")
    args = ap.parse_args()

    cfg = json.loads((BOARD_DIR / "config.json").read_text(encoding="utf-8"))
    extra = load_local_conf(Path(args.conf))
    for env, symbol in ENV_STRING_KEYS.items():
        if os.environ.get(env):
            extra.append(f"{symbol}={quote(os.environ[env])}")

    shown = [re.sub(r'(TOKEN|PASSWORD)=.*', r'\1=***', e) for e in extra]
    print("[shuttle] private settings:", ", ".join(shown) if shown else "(none - using Kconfig defaults)")
    for build in cfg["builds"]:
        build["sdkconfig_append"] = merge(build.get("sdkconfig_append", []), extra)
    (BOARD_DIR / LOCAL_JSON).write_text(json.dumps(cfg, indent=4, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.dry_run:
        return

    cmd = [sys.executable, str(ROOT / "scripts" / "build.py"), BOARD, "-c", LOCAL_JSON,
           "--name", VARIANT, "--language", args.language]
    if args.zip:
        cmd.append("--zip")
    print("[shuttle]", " ".join(cmd))
    subprocess.run(cmd, cwd=ROOT, check=True)
    print("[shuttle] done: build/merged-binary.bin  (esptool.py write_flash 0x0 build/merged-binary.bin)")


if __name__ == "__main__":
    main()
