#!/usr/bin/env python3
"""Generate PWA icons for the Madise Ilmaradar weather app.

Source: source-house.png — line-art illustration of the country house
at Madise (windows softly lit, on a dark navy background with a thin
cyan-blue rounded frame).

This script resizes the source to all the standard PWA / iOS / Android
icon sizes. Non-maskable variants keep the source's existing rounded
frame; the maskable variant adds extra padding inside a safe zone so
Android's adaptive-icon mask can crop the edges without clipping the
house artwork.

Run from this directory: python3 generate_icons.py
"""

from __future__ import annotations

from pathlib import Path
from PIL import Image, ImageDraw

OUT_DIR = Path(__file__).parent
SOURCE = OUT_DIR / "source-house.png"
BG_DEEP = (6, 13, 20)  # matches app's --bg variable so icons feel native


def color_to_alpha(img: Image.Image, target: tuple[int, int, int] = (255, 255, 255)) -> Image.Image:
    """Replace 'target' colour with transparency, GIMP-style.

    Useful when the source was exported with a vignette that fades to a
    flat colour (e.g. white) instead of transparency. We compute an alpha
    channel where pixels matching `target` become transparent and pixels
    far from target stay opaque, and un-mix the colour so the original
    artwork shows cleanly on any background.
    """
    try:
        import numpy as np
    except ImportError:
        # numpy not available — fall back to no conversion
        return img.convert("RGBA")
    arr = np.array(img.convert("RGBA"), dtype=np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    tr, tg, tb = target
    # Per-channel "distance from target" normalised. For each channel,
    # if pixel equals target, contribution is 0; if pixel is opposite,
    # contribution is 1. Final alpha = max of the three.
    da = np.maximum.reduce([
        np.abs(r - tr) / max(tr, 255 - tr),
        np.abs(g - tg) / max(tg, 255 - tg),
        np.abs(b - tb) / max(tb, 255 - tb),
    ])
    safe = np.clip(da, 0.001, 1.0)
    new_r = np.clip((r - tr * (1 - da)) / safe, 0, 255)
    new_g = np.clip((g - tg * (1 - da)) / safe, 0, 255)
    new_b = np.clip((b - tb * (1 - da)) / safe, 0, 255)
    new_a = np.clip(da * 255, 0, 255)
    out = np.stack([new_r, new_g, new_b, new_a], axis=-1).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def load_source() -> Image.Image:
    if not SOURCE.exists():
        raise SystemExit(f"Source file missing: {SOURCE}")
    img = Image.open(SOURCE).convert("RGBA")
    # If the source has white-ish corners, treat it as a vignette-on-white
    # export and convert white to transparency so the artwork floats on
    # any background. Otherwise leave as-is.
    px = img.load()
    corner = px[5, 5]
    if all(c > 230 for c in corner[:3]):
        print("  source has white corners — applying color-to-alpha")
        img = color_to_alpha(img, target=(255, 255, 255))
    return img


def square_crop(img: Image.Image) -> Image.Image:
    w, h = img.size
    if w == h:
        return img
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    return img.crop((left, top, left + side, top + side))


def make_icon(source: Image.Image, size: int, maskable: bool = False) -> Image.Image:
    """Produce a square PNG at the target size.

    - non-maskable: source is resized to fit the full square. Source
      already has its own rounded frame, so iOS/browser rounded masks
      overlay cleanly.
    - maskable: source is scaled down to 78% of the canvas and centered
      on an opaque dark background so Android's adaptive-icon mask
      (which can crop up to 10% from each edge) won't clip the house.
    """
    src = square_crop(source)
    if not maskable:
        return src.resize((size, size), Image.LANCZOS)

    canvas = Image.new("RGBA", (size, size), BG_DEEP + (255,))
    inner = int(size * 0.78)
    resized = src.resize((inner, inner), Image.LANCZOS)
    offset = (size - inner) // 2
    canvas.paste(resized, (offset, offset), resized)
    return canvas


def main() -> None:
    src = load_source()
    targets = [
        ("icon-180.png", 180, False),
        ("icon-192.png", 192, False),
        ("icon-512.png", 512, False),
        ("icon-512-maskable.png", 512, True),
        ("apple-touch-icon.png", 180, False),
        ("favicon-32.png", 32, False),
        ("favicon-16.png", 16, False),
        ("topbar-logo.png", 216, False),  # 72 px display × 3x DPR for iPhone Pro screens
    ]
    for name, size, maskable in targets:
        img = make_icon(src, size, maskable=maskable)
        img.save(OUT_DIR / name, "PNG", optimize=True)
        print(f"  wrote {name} ({size}x{size}{', maskable' if maskable else ''})")
    print("Done.")


if __name__ == "__main__":
    main()
