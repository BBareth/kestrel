"""Generate PWA icons + iOS splash screens (run once; outputs are committed).

    python scripts/make_icons.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent.parent / "frontend" / "public"
BG_TOP, BG_BOTTOM = (22, 31, 45), (10, 13, 17)
WHITE, GREEN, RED = (230, 234, 240), (31, 194, 126), (240, 70, 90)
SPLASH = ["1320x2868", "1206x2622", "1290x2796", "1179x2556", "1284x2778", "1170x2532", "1242x2688", "1125x2436", "828x1792", "750x1334"]


def gradient(size: tuple[int, int]) -> Image.Image:
    w, h = size
    img = Image.new("RGB", size, BG_BOTTOM)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(1, h - 1)
        c = tuple(int(BG_TOP[i] * (1 - t) + BG_BOTTOM[i] * t) for i in range(3))
        d.line([(0, y), (w, y)], fill=c)
    return img


def mark(size: int, mono: tuple[int, int, int] | None = None) -> Image.Image:
    """Geometric 'K': neutral spine, green arm up (long), red arm down (short)."""
    s = size * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = s / 100
    d.rounded_rectangle([22 * u, 14 * u, 38 * u, 86 * u], radius=3 * u, fill=mono or WHITE)
    d.polygon([(38 * u, 56 * u), (38 * u, 40 * u), (70 * u, 14 * u), (88 * u, 14 * u)], fill=mono or GREEN)
    d.polygon([(46 * u, 50 * u), (58 * u, 42 * u), (88 * u, 86 * u), (70 * u, 86 * u)], fill=mono or RED)
    return img.resize((size, size), Image.LANCZOS)


def icon(size: int, scale: float = 0.62, rounded: bool = False) -> Image.Image:
    base = gradient((size, size)).convert("RGBA")
    m = mark(int(size * scale))
    off = (size - m.width) // 2
    base.alpha_composite(m, (off, off))
    if rounded:
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=int(size * 0.22), fill=255)
        base.putalpha(mask)
    return base


def main() -> None:
    (OUT / "icons").mkdir(parents=True, exist_ok=True)
    (OUT / "splash").mkdir(parents=True, exist_ok=True)
    icon(192, rounded=True).save(OUT / "icons/icon-192.png", optimize=True)
    icon(512, rounded=True).save(OUT / "icons/icon-512.png", optimize=True)
    icon(512, scale=0.5).convert("RGB").save(OUT / "icons/maskable-512.png", optimize=True)  # full-bleed, safe zone
    icon(180, scale=0.6).convert("RGB").save(OUT / "icons/apple-touch-icon.png", optimize=True)  # iOS: opaque square
    icon(32, scale=0.8, rounded=True).save(OUT / "icons/favicon-32.png", optimize=True)
    mark(96, mono=(255, 255, 255)).save(OUT / "icons/badge-96.png", optimize=True)
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 10)
    except OSError:
        font = None
    for spec in SPLASH:
        w, h = map(int, spec.split("x"))
        img = gradient((w, h)).convert("RGBA")
        m = mark(int(w * 0.26))
        img.alpha_composite(m, ((w - m.width) // 2, int(h * 0.40) - m.height // 2))
        if font:
            f = ImageFont.truetype(font.path, int(w * 0.052))
            d = ImageDraw.Draw(img)
            text = "K E S T R E L"
            tw = d.textlength(text, font=f)
            d.text(((w - tw) / 2, int(h * 0.40) + m.height // 2 + int(w * 0.06)), text, font=f, fill=WHITE)
            f2 = ImageFont.truetype(font.path, int(w * 0.03))
            sub = "BTC PERP DESK · PAPER-FIRST"
            sw = d.textlength(sub, font=f2)
            d.text(((w - sw) / 2, int(h * 0.40) + m.height // 2 + int(w * 0.14)), sub, font=f2, fill=(139, 149, 165))
        img.convert("RGB").save(OUT / f"splash/{spec}.png", optimize=True)
    print("icons + splash written to", OUT)


if __name__ == "__main__":
    main()
