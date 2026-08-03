#!/usr/bin/env python3
"""Generate deterministic desktop icons from the canonical Gongge SVG mark."""

from __future__ import annotations

from pathlib import Path
from shutil import copyfile

from PIL import Image, ImageDraw


REPO = Path(__file__).resolve().parents[1]
SOURCE = REPO / "frontend-enterprise" / "src" / "assets" / "brand" / "gongge-mark.svg"
ASSETS = REPO / "packaging" / "assets"
SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256, 512, 1024)


def _mix(start: tuple[int, ...], end: tuple[int, ...], ratio: float) -> tuple[int, ...]:
    return tuple(round(left + (right - left) * ratio) for left, right in zip(start, end))


def render_master() -> Image.Image:
    size = 1024
    scale = size / 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    surface = Image.new("RGBA", (size, size))
    surface_pixels = surface.load()
    for y in range(size):
        for x in range(size):
            ratio = min(1.0, max(0.0, (x + y) / (2 * (size - 1))))
            surface_pixels[x, y] = _mix((22, 119, 255, 255), (18, 55, 184, 255), ratio)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size - 1, size - 1), radius=16 * scale, fill=255)
    image.alpha_composite(Image.composite(surface, Image.new("RGBA", image.size), mask))

    draw = ImageDraw.Draw(image)
    pale = (235, 247, 255, 255)
    muted = (217, 237, 255, 215)
    radius = round(3.5 * scale)
    cells = (
        ((17, 15, 29, 27), pale),
        ((35, 15, 47, 27), muted),
        ((17, 33, 29, 49), muted),
        ((35, 33, 47, 49), pale),
    )
    for (left, top, right, bottom), color in cells:
        draw.rounded_rectangle(
            tuple(round(value * scale) for value in (left, top, right, bottom)),
            radius=radius,
            fill=color,
        )
    accent = (125, 216, 255, 255)
    draw.line((23 * scale, 24 * scale, 41 * scale, 24 * scale), fill=accent, width=round(2 * scale))
    draw.line((23 * scale, 40 * scale, 41 * scale, 40 * scale), fill=accent, width=round(2 * scale))
    draw.ellipse((30 * scale, 28 * scale, 34 * scale, 32 * scale), fill=accent)
    return image


def main() -> None:
    ASSETS.mkdir(parents=True, exist_ok=True)
    copyfile(SOURCE, ASSETS / "gongge-xuban-mark.svg")
    master = render_master()
    images = {
        size: master.resize((size, size), Image.Resampling.LANCZOS)
        for size in SIZES
    }
    images[128].save(ASSETS / "gongge-xuban.png", format="PNG")
    images[256].save(
        ASSETS / "gongge-xuban.ico",
        format="ICO",
        sizes=[(size, size) for size in SIZES if size <= 256],
    )
    images[1024].save(ASSETS / "gongge-xuban.icns", format="ICNS")


if __name__ == "__main__":
    main()
