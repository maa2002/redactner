"""Draw the application icon: lines of text on a blue tile, with one word blacked out.

Run ``python packaging/make_icon.py`` to regenerate ``packaging/icon.ico`` (requires Pillow). The shapes are
drawn at four times the final size and scaled down, which keeps the edges smooth at every icon size.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 256
SCALE = 4
TILE = "#2457d6"       # the interface's primary colour
TEXT = "#ffffff"
REDACTED = "#0b1220"
ICON_SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def draw_icon() -> Image.Image:
    full = SIZE * SCALE
    image = Image.new("RGBA", (full, full), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)

    def box(x0: float, y0: float, x1: float, y1: float, radius: float, colour: str) -> None:
        draw.rounded_rectangle([x0 * SCALE, y0 * SCALE, x1 * SCALE, y1 * SCALE], radius=radius * SCALE, fill=colour)

    box(8, 8, 248, 248, 54, TILE)
    # three lines of text; most of the middle line is covered by a redaction bar
    box(56, 68, 200, 96, 14, TEXT)
    box(56, 114, 156, 142, 6, REDACTED)
    box(168, 114, 200, 142, 14, TEXT)
    box(56, 160, 164, 188, 14, TEXT)
    return image.resize((SIZE, SIZE), Image.LANCZOS)


def main() -> None:
    folder = Path(__file__).resolve().parent
    icon = draw_icon()
    icon.save(folder / "icon.ico", sizes=[(s, s) for s in ICON_SIZES])
    print("Wrote", (folder / "icon.ico").name, "with sizes", ICON_SIZES)


if __name__ == "__main__":
    main()
