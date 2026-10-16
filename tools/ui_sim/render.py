#!/usr/bin/env python3
"""Turn ui_sim PGM frames into docs/ui/screens.png (gallery) and animated GIFs."""
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

src, dst = Path(sys.argv[1]), Path(sys.argv[2])
dst.mkdir(parents=True, exist_ok=True)
frames = sorted(src.glob("*.pgm"))
SCALE = 2
PAPER = (236, 233, 222)  # e-paper "white"
INK = (34, 34, 38)


def paperize(img: Image.Image) -> Image.Image:
    img = img.convert("L").resize((img.width * SCALE, img.height * SCALE), Image.NEAREST)
    rgb = Image.new("RGB", img.size, PAPER)
    rgb.paste(Image.new("RGB", img.size, INK), mask=img.point(lambda v: 255 - v))
    framed = Image.new("RGB", (rgb.width + 24, rgb.height + 24), (25, 25, 28))
    framed.paste(rgb, (12, 12))
    return framed


# gallery: first frame of each state
seen = {}
for f in frames:
    key = f.stem.rsplit("_", 1)[0] if f.stem[-1].isdigit() and "_" in f.stem[3:] else f.stem
    if key.endswith("_blink"):
        continue
    seen.setdefault(key, f)
tiles = [(k, paperize(Image.open(f))) for k, f in seen.items()]
cols = 4
tw, th = tiles[0][1].size
sheet = Image.new("RGB", (cols * (tw + 16) + 16, ((len(tiles) + cols - 1) // cols) * (th + 40) + 16), (250, 250, 250))
d = ImageDraw.Draw(sheet)
for i, (k, t) in enumerate(tiles):
    x, y = 16 + (i % cols) * (tw + 16), 16 + (i // cols) * (th + 40)
    sheet.paste(t, (x, y))
    d.text((x, y + th + 6), k[3:], fill=(60, 60, 60))
sheet.save(dst / "screens.png")
print("wrote", dst / "screens.png")

# animations
for anim in ("03_listening", "04_thinking", "05_acting", "07_speaking"):
    fs = sorted(src.glob(f"{anim}_*.pgm"))
    if len(fs) > 1:
        imgs = [paperize(Image.open(f)) for f in fs]
        out = dst / f"{anim[3:]}.gif"
        imgs[0].save(out, save_all=True, append_images=imgs[1:], duration=800, loop=0)
        print("wrote", out)
