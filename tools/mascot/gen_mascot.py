#!/usr/bin/env python3
"""Shuttle mascot ("小搞", an original pixel-art octopus) -> LVGL 1-bit images.

Every frame is drawn on a 26x20 pixel grid (black body, white eyes) and scaled x3 to 78x60,
which keeps the chunky pixel look and is razor sharp on a black/white e-paper.

    python tools/mascot/gen_mascot.py            # writes the C header + preview PNG

Outputs
  firmware/main/boards/shuttle/epaper-1.54-v2/mascot_frames.h   (LV_COLOR_FORMAT_I1 images)
  docs/ui/mascot_sheet.png                                       (all moods, for review)
"""
from __future__ import annotations

from pathlib import Path

W, H, SCALE = 26, 20, 3
REPO = Path(__file__).resolve().parents[2]
HEADER = REPO / "firmware/main/boards/shuttle/epaper-1.54-v2/mascot_frames.h"
SHEET = REPO / "docs/ui/mascot_sheet.png"

# Body: 18 wide, dome head + 4 curling tentacles, placed at (3, 4). '#' = black.
HEAD = [
    "......######......",
    "....##########....",
    "...############...",
    "..##############..",
    ".################.",
    ".################.",
    "##################",
    "##################",
    "##################",
    "##################",
    ".################.",
]
LEGS_A = [  # tentacles, curls outward
    "..##..##..##..##..",
    "..##..##..##..##..",
    ".##...##..##...##.",
    ".#....#....#....#.",
]
LEGS_B = [  # wiggle frame: tips curl the other way
    "..##..##..##..##..",
    "..##..##..##..##..",
    "..##..##..##..##..",
    "...#...#...#...#..",
]
BX, BY = 4, 3
EYE_L, EYE_R = (BX + 5, BY + 5), (BX + 11, BY + 5)  # top-left of each 2x3 eye


class Grid:
    def __init__(self):
        self.px = [[0] * W for _ in range(H)]

    def set(self, x, y, v=1):
        if 0 <= x < W and 0 <= y < H:
            self.px[y][x] = v

    def rows(self, x0, y0, rows, v=1):
        for dy, row in enumerate(rows):
            for dx, ch in enumerate(row):
                if ch == "#":
                    self.set(x0 + dx, y0 + dy, v)

    def rect(self, x, y, w, h, v=1):
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.set(xx, yy, v)


def body(legs=LEGS_A) -> Grid:
    g = Grid()
    g.rows(BX, BY, HEAD)
    g.rows(BX, BY + len(HEAD), legs)
    return g


# ---- eyes: white (0) on the black head ---------------------------------------------------
def eyes_normal(g, dx=0, dy=0, h=3):
    for ex, ey in (EYE_L, EYE_R):
        g.rect(ex + dx, ey + dy, 2, h, 0)


def eyes_closed(g):  # sleeping: flat lines
    for ex, ey in (EYE_L, EYE_R):
        g.rect(ex - 1, ey + 2, 4, 1, 0)


def eyes_happy(g):  # ^ ^
    for ex, ey in (EYE_L, EYE_R):
        g.rows(ex - 1, ey + 1, [".##.", "#..#"], 0)


def eyes_focus(g):  # concentrating: lower lids
    for ex, ey in (EYE_L, EYE_R):
        g.rect(ex, ey + 1, 2, 2, 0)


def eyes_x(g):
    for ex, ey in (EYE_L, EYE_R):
        g.rows(ex - 1, ey, ["#.#", ".#.", "#.#"], 0)


def mouth_open(g):
    g.rect(BX + 8, BY + 9, 2, 1, 0)


# ---- accessories (black, around the body) ------------------------------------------------
def waves(g, n):
    for i in range(n):
        o = 2 * i
        g.rows(2 - o, BY + 5, [".#", "#.", "#.", ".#"])
        g.rows(22 + o, BY + 5, ["#.", ".#", ".#", "#."])


def thought(g, n):
    if n >= 1:
        g.set(19, 3)
    if n >= 2:
        g.rect(20, 1, 2, 2)
    if n >= 3:
        g.rect(23, 0, 3, 3)


def zzz(g):
    g.rows(20, 0, ["###", "..#", ".#.", "###"])


def sparkle(g, x, y):
    g.rows(x - 1, y - 1, [".#.", "###", ".#."])


def sweat(g):
    g.rows(1, BY + 1, [".#.", "###", "###", ".#."])


def phone(g, up):
    y = BY + 8 if up else BY + 9
    g.rect(21, y, 4, 7)
    g.rect(22, y + 1, 2, 4, 0)


# ---- moods (order must match ShuttleUi::Mood) -------------------------------------------
MOODS = ["idle", "sleep", "connecting", "listening", "thinking", "acting", "speaking", "done", "error",
         "offline"]


def frames():
    out = {}

    g = body(); eyes_normal(g)
    g2 = body(); eyes_closed(g2)  # occasional blink
    out["idle"] = [g, g2]

    g = body(); eyes_closed(g); zzz(g)
    out["sleep"] = [g]

    g = body(); eyes_normal(g, dx=-1)
    g2 = body(); eyes_normal(g2, dx=1)
    out["connecting"] = [g, g2]

    g = body(); eyes_normal(g, dy=-1, h=4); waves(g, 1)
    g2 = body(); eyes_normal(g2, dy=-1, h=4); waves(g2, 2)
    out["listening"] = [g, g2]

    res = []
    for n in (1, 2, 3):
        g = body(); eyes_normal(g, dx=1, dy=-1); thought(g, n); res.append(g)
    out["thinking"] = res

    g = body(LEGS_A); eyes_focus(g); phone(g, True)
    g2 = body(LEGS_B); eyes_focus(g2); phone(g2, False)
    out["acting"] = [g, g2]

    g = body(); eyes_normal(g)
    g2 = body(); eyes_normal(g2); mouth_open(g2)
    out["speaking"] = [g, g2]

    g = body(); eyes_happy(g); sparkle(g, 2, 2); sparkle(g, 23, 3)
    out["done"] = [g]

    g = body(); eyes_x(g); sweat(g)
    out["error"] = [g]

    g = body(); eyes_normal(g, dy=1, h=2)
    out["offline"] = [g]
    assert list(out) == MOODS
    return out


def scaled_bits(g: Grid) -> list[list[int]]:
    return [[g.px[y // SCALE][x // SCALE] for x in range(W * SCALE)] for y in range(H * SCALE)]


def to_c(name: str, bits: list[list[int]]) -> str:
    w, h = len(bits[0]), len(bits)
    stride = (w + 7) // 8
    data = [0xFF, 0xFF, 0xFF, 0x00,  # index 0: transparent (white)
            0x00, 0x00, 0x00, 0xFF]  # index 1: black, opaque (BGRA)
    for row in bits:
        for i in range(stride):
            byte = 0
            for b in range(8):
                x = i * 8 + b
                if x < w and row[x]:
                    byte |= 0x80 >> b
            data.append(byte)
    lines = ", ".join(f"0x{v:02x}" for v in data)
    return (f"static const uint8_t {name}_map[] = {{{lines}}};\n"
            f"static const lv_image_dsc_t {name} = {{\n"
            f"    .header = {{.magic = LV_IMAGE_HEADER_MAGIC, .cf = LV_COLOR_FORMAT_I1, .flags = 0,\n"
            f"               .w = {w}, .h = {h}, .stride = {stride}, .reserved_2 = 0}},\n"
            f"    .data_size = sizeof({name}_map),\n"
            f"    .data = {name}_map,\n"
            f"    .reserved = nullptr,\n"
            f"    .reserved_2 = nullptr,\n"
            f"}};\n")


def main() -> None:
    moods = frames()
    parts = [
        "// Generated by tools/mascot/gen_mascot.py - do not edit by hand.\n",
        "// Shuttle mascot: original 26x20 pixel-art octopus scaled x3 (78x60), LVGL I1 images.\n",
        "#pragma once\n\n#include <lvgl.h>\n\n",
        f"#define MASCOT_W {W * SCALE}\n#define MASCOT_H {H * SCALE}\n\n",
    ]
    table = []
    for mood, gs in moods.items():
        names = []
        for i, g in enumerate(gs):
            n = f"mascot_{mood}_{i}"
            parts.append(to_c(n, scaled_bits(g)))
            names.append(n)
        parts.append(f"static const lv_image_dsc_t* const mascot_{mood}_frames[] = {{"
                     + ", ".join(f"&{n}" for n in names) + "};\n\n")
        table.append((mood, len(names)))
    parts.append("// mood name -> frames (order matches ShuttleUi::Mood)\n")
    HEADER.write_text("".join(parts), encoding="utf-8")
    print(f"wrote {HEADER.relative_to(REPO)} ({sum(n for _, n in table)} frames)")

    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return
    cell_w, cell_h = W * SCALE * 2 + 20, H * SCALE * 2 + 36
    cols = 6
    sheet_frames = [(m, i, g) for m, gs in moods.items() for i, g in enumerate(gs)]
    rows = (len(sheet_frames) + cols - 1) // cols
    img = Image.new("L", (cols * cell_w, rows * cell_h), 255)
    d = ImageDraw.Draw(img)
    for k, (m, i, g) in enumerate(sheet_frames):
        cx, cy = (k % cols) * cell_w + 10, (k // cols) * cell_h + 8
        for y, row in enumerate(scaled_bits(g)):
            for x, v in enumerate(row):
                if v:
                    d.rectangle([cx + 2 * x, cy + 2 * y, cx + 2 * x + 1, cy + 2 * y + 1], fill=0)
        d.text((cx, cy + H * SCALE * 2 + 6), f"{m}[{i}]", fill=0)
    SHEET.parent.mkdir(parents=True, exist_ok=True)
    img.save(SHEET)
    print(f"wrote {SHEET.relative_to(REPO)}")


if __name__ == "__main__":
    main()
