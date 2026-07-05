#!/usr/bin/env python3
"""Render the local Madise Ilmaradar app at common device viewports and
save full-page PNG screenshots under shots/.

Usage:
    source .venv/bin/activate
    python3 screenshots.py                    # all devices
    python3 screenshots.py iphone14 pixel7    # specific subset
    python3 screenshots.py --url http://localhost:8123  # custom URL

The dev server must already be running (python3 -m http.server 8123).
"""

import argparse
import asyncio
import sys
from pathlib import Path
from playwright.async_api import async_playwright

OUT_DIR = Path(__file__).parent / "shots"

# CSS-width × CSS-height in dp, plus DPR. The DPR mostly matters for
# image-rendering crispness; layout is driven by the width.
DEVICES = {
    "android-small":   {"w": 360, "h": 800,  "dpr": 3, "label": "360×800 (small Android, e.g. Galaxy A)"},
    "pixel7":          {"w": 393, "h": 851,  "dpr": 3, "label": "393×851 (Pixel 7, iPhone 14)"},
    "iphone14-plus":   {"w": 430, "h": 932,  "dpr": 3, "label": "430×932 (iPhone 14 Pro Max, S22 Ultra)"},
    "fold-folded":     {"w": 320, "h": 800,  "dpr": 3, "label": "320×800 (very narrow / folded foldable)"},
    "fold-unfolded":   {"w": 717, "h": 921,  "dpr": 2, "label": "717×921 (Galaxy Fold unfolded)"},
    "ipad-mini":       {"w": 768, "h": 1024, "dpr": 2, "label": "768×1024 (iPad mini portrait)"},
}


async def capture(url: str, names: list[str]) -> None:
    OUT_DIR.mkdir(exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch()
        for name in names:
            d = DEVICES[name]
            ctx = await browser.new_context(
                viewport={"width": d["w"], "height": d["h"]},
                device_scale_factor=d["dpr"],
                is_mobile=True,
                has_touch=True,
                user_agent=(
                    "Mozilla/5.0 (Linux; Android 14; Pixel 7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36"
                ),
            )
            page = await ctx.new_page()
            await page.goto(url, wait_until="networkidle", timeout=15000)
            # Give the app a moment to render its async API data
            await page.wait_for_timeout(2500)
            out = OUT_DIR / f"{name}.png"
            await page.screenshot(path=str(out), full_page=True)
            await ctx.close()
            print(f"  {name:16s} {d['label']:46s} → {out.name}")
        await browser.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://localhost:8123/")
    parser.add_argument("devices", nargs="*", help="device names (default: all)")
    args = parser.parse_args()

    names = args.devices or list(DEVICES)
    unknown = [n for n in names if n not in DEVICES]
    if unknown:
        print(f"Unknown device(s): {unknown}\nAvailable: {list(DEVICES)}", file=sys.stderr)
        return 2

    asyncio.run(capture(args.url, names))
    return 0


if __name__ == "__main__":
    sys.exit(main())
