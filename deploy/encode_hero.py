#!/usr/bin/env python3
"""Rebuild the splash-page hero from docs/assets/AbsoLoopOS.gif.

The README header is a 480×480 GIF (about 7 MB, 181 frames). This writes a
transparent animated WebP plus a still of the fullest frame for
prefers-reduced-motion. Requires ffmpeg and Pillow. Output lands in site/assets/.

    python3 deploy/encode_hero.py
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
GIF = ROOT / "docs" / "assets" / "AbsoLoopOS.gif"
OUT_ANIM = ROOT / "site" / "assets" / "absoloop-os.webp"
OUT_STILL = ROOT / "site" / "assets" / "absoloop-os-still.webp"
FPS = 12
# Frame 89 of the 18 fps source is the fullest mark (measured by ink).
STILL_AT = 59
# Vertical crop of the 480px frame. Ink stays inside y=46..397.
CROP = (0, 36, 480, 408)


def key_near_black(im: Image.Image) -> Image.Image:
    im = im.convert("RGBA")
    pixels = im.load()
    width, height = im.size
    for y in range(height):
        for x in range(width):
            red, green, blue, _alpha = pixels[x, y]
            if red < 16 and green < 16 and blue < 16:
                pixels[x, y] = (0, 0, 0, 0)
    return im


def main() -> int:
    if not GIF.is_file():
        print(f"missing {GIF}", file=sys.stderr)
        return 1
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp)
        subprocess.check_call(
            [
                "ffmpeg",
                "-y",
                "-v",
                "error",
                "-i",
                str(GIF),
                "-vf",
                f"fps={FPS}",
                str(folder / "f%03d.png"),
            ]
        )
        frames = [key_near_black(Image.open(path)).crop(CROP) for path in sorted(folder.glob("*.png"))]
    if not frames:
        print("no frames", file=sys.stderr)
        return 1
    OUT_ANIM.parent.mkdir(parents=True, exist_ok=True)
    # Open on the fullest frame so the mark is recognizable on first paint,
    # then play the same assemble / release cycle.
    origin = min(STILL_AT, len(frames) - 1)
    ordered = frames[origin:] + frames[:origin]
    ordered[0].save(
        OUT_ANIM,
        format="WEBP",
        save_all=True,
        append_images=ordered[1:],
        duration=round(1000 / FPS),
        loop=0,
        quality=78,
        method=6,
    )
    frames[origin].save(OUT_STILL, format="WEBP", quality=86, method=6)
    print(f"wrote {OUT_ANIM} ({OUT_ANIM.stat().st_size} bytes, {len(frames)} frames)")
    print(f"wrote {OUT_STILL} ({OUT_STILL.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
