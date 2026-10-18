#!/usr/bin/env python3
"""Decode e-paper frames logged by the firmware into PNGs: every flushed frame with
CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP=y, or on request with the debug console command "dump".

The firmware logs a frame as base64 1-bpp chunks (seq restarts at every boot):
    I (1234) ShuttleDisplay: SCRDUMP <seq> <w>x<h> <part>/<parts> <base64...>

Usage:
    python tools/screen_dump.py serial.log                 # newest frame -> screen.png
    python tools/screen_dump.py serial.log --all -o out/   # every frame -> out/frame_<n>.png (log order)
    python tools/screen_dump.py serial.log --scale 3       # bigger PNG (pixels stay sharp)
"""
from __future__ import annotations

import argparse
import base64
import re
import sys
from pathlib import Path

from PIL import Image

LINE = re.compile(r"SCRDUMP (\d+) (\d+)x(\d+) (\d+)/(\d+) ([A-Za-z0-9+/=]+)")


def parse(text: str) -> list[Image.Image]:
    """Complete frames in log order. A frame's chunks are logged back to back; frames cut off
    (log started mid-frame, reboot) are skipped."""
    frames: list[Image.Image] = []
    chunks: dict[int, str] = {}
    current: tuple[int, int, int, int] | None = None  # seq, w, h, parts
    for m in LINE.finditer(text):
        seq, w, h, part, total, data = int(m[1]), int(m[2]), int(m[3]), int(m[4]), int(m[5]), m[6]
        if current is None or current[0] != seq:
            current, chunks = (seq, w, h, total), {}
        chunks[part] = data
        if len(chunks) == total:
            raw = base64.b64decode("".join(chunks[i] for i in range(1, total + 1)))
            bits = Image.frombytes("1", (w, h), raw)  # 1 = black in the dump, PIL "1": 1 = white
            frames.append(Image.eval(bits.convert("L"), lambda v: 255 - v))
            current = None
    return frames


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", help="serial log file ('-' = stdin)")
    ap.add_argument("-o", "--out", default="screen.png", help="output PNG (or directory with --all)")
    ap.add_argument("--all", action="store_true", help="write every complete frame")
    ap.add_argument("--scale", type=int, default=2)
    args = ap.parse_args()
    text = sys.stdin.read() if args.log == "-" else Path(args.log).read_text(encoding="utf-8", errors="replace")
    frames = parse(text)
    if not frames:
        print("no complete SCRDUMP frame found (console \"dump\", or CONFIG_SHUTTLE_DEBUG_SCREEN_DUMP)", file=sys.stderr)
        return 1

    def save(img: Image.Image, path: Path) -> None:
        if args.scale > 1:
            img = img.resize((img.width * args.scale, img.height * args.scale), Image.NEAREST)
        img.save(path)
        print(path)

    if args.all:
        out = Path(args.out if args.out != "screen.png" else "frames")
        out.mkdir(parents=True, exist_ok=True)
        for i, frame in enumerate(frames, 1):
            save(frame, out / f"frame_{i:04d}.png")
    else:
        save(frames[-1], Path(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
