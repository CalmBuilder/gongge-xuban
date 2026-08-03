#!/usr/bin/env python3
"""Generate a lightweight Gongge-Xuban DMG background without external deps."""

from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path


WIDTH = 840
HEIGHT = 360


def _mix(a: int, b: int, t: float) -> int:
    return round(a + (b - a) * t)


def _inside_round_rect(x: int, y: int, left: int, top: int, right: int, bottom: int, radius: int) -> bool:
    if left + radius <= x <= right - radius and top <= y <= bottom:
        return True
    if left <= x <= right and top + radius <= y <= bottom - radius:
        return True
    for cx, cy in (
        (left + radius, top + radius),
        (right - radius, top + radius),
        (left + radius, bottom - radius),
        (right - radius, bottom - radius),
    ):
        if (x - cx) * (x - cx) + (y - cy) * (y - cy) <= radius * radius:
            return True
    return False


def _blend(pixel: tuple[int, int, int, int], color: tuple[int, int, int], alpha: float) -> tuple[int, int, int, int]:
    r, g, b, a = pixel
    cr, cg, cb = color
    return (_mix(r, cr, alpha), _mix(g, cg, alpha), _mix(b, cb, alpha), a)


def _soft_rect_alpha(x: float, y: float, left: float, top: float, right: float, bottom: float, radius: float) -> float:
    qx = abs(x - (left + right) / 2) - ((right - left) / 2 - radius)
    qy = abs(y - (top + bottom) / 2) - ((bottom - top) / 2 - radius)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    dist = outside + inside - radius
    return max(0.0, min(1.0, 1.0 - dist / 2.0))


def _soft_circle_alpha(x: float, y: float, cx: float, cy: float, radius: float, feather: float) -> float:
    dist = math.hypot(x - cx, y - cy)
    return max(0.0, min(1.0, (radius - dist) / feather))


def _distance_to_segment(px: float, py: float, ax: float, ay: float, bx: float, by: float) -> float:
    vx = bx - ax
    vy = by - ay
    wx = px - ax
    wy = py - ay
    length_sq = vx * vx + vy * vy
    if length_sq == 0:
        return math.hypot(px - ax, py - ay)
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / length_sq))
    cx = ax + t * vx
    cy = ay + t * vy
    return math.hypot(px - cx, py - cy)


def _inside_triangle(px: float, py: float, points: tuple[tuple[float, float], ...]) -> bool:
    (x1, y1), (x2, y2), (x3, y3) = points
    d1 = (px - x2) * (y1 - y2) - (x1 - x2) * (py - y2)
    d2 = (px - x3) * (y2 - y3) - (x2 - x3) * (py - y3)
    d3 = (px - x1) * (y3 - y1) - (x3 - x1) * (py - y1)
    has_neg = d1 < 0 or d2 < 0 or d3 < 0
    has_pos = d1 > 0 or d2 > 0 or d3 > 0
    return not (has_neg and has_pos)


def generate(path: Path) -> None:
    pixels: list[tuple[int, int, int, int]] = []
    arrow_color = (64, 86, 120)
    arrow = ((340.0, 179.0), (500.0, 179.0))
    arrow_head = ((500.0, 179.0), (474.0, 160.0), (474.0, 198.0))

    for y in range(HEIGHT):
        y_t = y / max(HEIGHT - 1, 1)
        for x in range(WIDTH):
            x_t = x / max(WIDTH - 1, 1)
            base = (
                _mix(248, 235, y_t),
                _mix(251, 243, y_t),
                _mix(253, 248, y_t),
                255,
            )

            # Full-width calm surface. The Finder window is wider on recent macOS,
            # so the art intentionally covers 840pt instead of leaving a white strip.
            if y < 52:
                base = _blend(base, (230, 237, 247), 0.22 * (1 - y / 52))
            if x_t > 0.72:
                base = _blend(base, (238, 247, 250), 0.14 * ((x_t - 0.72) / 0.28))
            if x_t < 0.26 and y_t > 0.42:
                base = _blend(base, (239, 245, 255), 0.10 * (1 - x_t / 0.26) * ((y_t - 0.42) / 0.58))

            # One glass-like lane holds the drag action without boxing the icons in
            # oversized cards.
            lane_alpha = _soft_rect_alpha(x + 0.5, y + 0.5, 106, 86, 734, 272, 34)
            if lane_alpha:
                base = _blend(base, (255, 255, 255), 0.54 * lane_alpha)
                edge_alpha = min(lane_alpha, 1 - _soft_rect_alpha(x + 0.5, y + 0.5, 109, 89, 731, 269, 31))
                if edge_alpha:
                    base = _blend(base, (209, 221, 236), 0.38 * edge_alpha)

            # Gentle focus behind each Finder icon/label.
            for cx, cy in ((230.0, 178.0), (610.0, 178.0)):
                halo = _soft_circle_alpha(x + 0.5, y + 0.5, cx, cy, 118, 42)
                if halo:
                    base = _blend(base, (255, 255, 255), 0.30 * halo)
                core = _soft_circle_alpha(x + 0.5, y + 0.5, cx, cy, 74, 18)
                if core:
                    base = _blend(base, (249, 253, 255), 0.34 * core)

            distance = _distance_to_segment(x + 0.5, y + 0.5, *arrow[0], *arrow[1])
            if distance <= 4.0:
                base = _blend(base, arrow_color, 0.18)
            if distance <= 2.0:
                base = _blend(base, arrow_color, 0.54)
            if _inside_triangle(x + 0.5, y + 0.5, arrow_head):
                base = _blend(base, arrow_color, 0.54)

            # Low, quiet brand accent. It stays below Finder labels.
            if y > 300:
                base = _blend(base, (42, 115, 205), 0.055 * ((y - 300) / 60))
            if x_t > 0.62 and y_t > 0.68:
                base = _blend(base, (0, 182, 192), 0.030 * (x_t - 0.62) * (y_t - 0.68) * 8)

            pixels.append(base)

    raw = bytearray()
    for y in range(HEIGHT):
        raw.append(0)
        row = pixels[y * WIDTH : (y + 1) * WIDTH]
        for r, g, b, a in row:
            raw.extend((r, g, b, a))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", WIDTH, HEIGHT, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: make_dmg_background.py <output.png>", file=sys.stderr)
        return 2
    generate(Path(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
