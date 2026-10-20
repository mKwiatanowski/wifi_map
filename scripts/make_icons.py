"""Generate the PWA icons (simple house glyph) into src/homesense/web/static/icons/.

Dev-only helper: needs Pillow (`pip install pillow`), which is not a runtime dependency.
Usage: python scripts/make_icons.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

ICONS = Path(__file__).resolve().parent.parent / "src" / "homesense" / "web" / "static" / "icons"
BACKGROUND = (15, 118, 110, 255)  # teal, matches --accent in app.css
FOREGROUND = (255, 255, 255, 255)
SUPERSAMPLE = 4


def house(size: int) -> Image.Image:
    """A rounded square with a white house; drawn large and downscaled for smooth edges."""
    s = size * SUPERSAMPLE
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, s - 1, s - 1), radius=s // 5, fill=BACKGROUND)

    def p(x: float, y: float) -> tuple[float, float]:
        return x * s, y * s

    d.polygon([p(0.5, 0.2), p(0.82, 0.48), p(0.18, 0.48)], fill=FOREGROUND)  # roof
    d.rectangle((*p(0.27, 0.47), *p(0.73, 0.8)), fill=FOREGROUND)  # walls
    d.rectangle((*p(0.44, 0.6), *p(0.56, 0.8)), fill=BACKGROUND)  # door
    return img.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    ICONS.mkdir(parents=True, exist_ok=True)
    for size in (192, 512):
        house(size).save(ICONS / f"icon-{size}.png", optimize=True)
        print(f"wrote {ICONS / f'icon-{size}.png'}")


if __name__ == "__main__":
    main()
