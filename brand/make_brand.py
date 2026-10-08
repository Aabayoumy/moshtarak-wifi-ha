#!/usr/bin/env python3
"""Generate the brand assets for the MTTL-W01 WiFi integration.

Hand-built, nothing scraped. Writes real PNGs using only zlib and struct, so it
runs anywhere Python does and needs no image library.

The mark: the IEC 60417 power symbol (a ring with a 90 degree gap at the top,
plus a vertical bar through it) in white on an indigo -> teal vertical gradient.

Supersampled 3x and box-filtered down, because a 1-sample-per-pixel ring at this
size comes out visibly jagged and a jagged logo is the first thing anyone
notices.
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path

INDIGO = (0x1E, 0x3A, 0x8A)
TEAL = (0x0D, 0x94, 0x88)
WHITE = (0xFF, 0xFF, 0xFF)

SS = 3  # supersample factor


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path: Path, width: int, height: int, pixels: list[list[tuple[int, int, int]]]) -> None:
    """Write 8-bit RGB pixels as a PNG."""
    raw = bytearray()
    for row in pixels:
        raw.append(0)  # filter type 0 (None) for each scanline
        for r, g, b in row:
            raw += bytes((r, g, b))

    png = b"\x89PNG\r\n\x1a\n"
    png += _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += _chunk(b"IEND", b"")
    path.write_bytes(png)


def _blend(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return (
        round(a[0] + (b[0] - a[0]) * t),
        round(a[1] + (b[1] - a[1]) * t),
        round(a[2] + (b[2] - a[2]) * t),
    )


def render(size: int) -> list[list[tuple[int, int, int]]]:
    """Render the mark at `size` x `size`, centred.

    Centred for the logo too. An earlier version offset the ring to 30% of the
    width to make a wide lockup, which left 70% of the tile as empty gradient -
    a worse icon than the square version, and one more thing to get wrong.
    """
    w = h = size
    rows: list[list[tuple[int, int, int]]] = []

    cx, cy = w / 2, h / 2
    radius = h * 0.26          # ring radius
    thickness = h * 0.085

    bar_half = thickness / 2
    bar_top = cy - radius - thickness * 0.35
    bar_bottom = cy + radius * 0.42

    # The 90 degree gap, centred on straight up.
    gap_half_deg = 46.0

    for y in range(h):
        row: list[tuple[int, int, int]] = []
        for x in range(w):
            acc_r = acc_g = acc_b = 0
            for sy in range(SS):
                for sx in range(SS):
                    fx = x + (sx + 0.5) / SS
                    fy = y + (sy + 0.5) / SS

                    # Background: vertical gradient.
                    t = fy / h
                    bg = _blend(INDIGO, TEAL, t)

                    # Foreground mask: the power glyph.
                    dx, dy = fx - cx, fy - cy
                    dist = math.hypot(dx, dy)

                    on_ring = False
                    if radius - thickness / 2 <= dist <= radius + thickness / 2:
                        # Angle measured from straight up, clockwise.
                        ang = math.degrees(math.atan2(dx, -dy))
                        if ang > 180:
                            ang -= 360
                        on_ring = abs(ang) > gap_half_deg

                    on_bar = (abs(dx) <= bar_half) and (bar_top <= fy <= bar_bottom)

                    colour = WHITE if (on_ring or on_bar) else bg
                    acc_r += colour[0]
                    acc_g += colour[1]
                    acc_b += colour[2]

            n = SS * SS
            row.append((acc_r // n, acc_g // n, acc_b // n))
        rows.append(row)
    return rows


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("brand")
    out_dir.mkdir(parents=True, exist_ok=True)

    icon = render(256)
    write_png(out_dir / "icon.png", 256, 256, icon)
    print(f"wrote {out_dir / 'icon.png'} (256x256)")

    logo = render(512)
    write_png(out_dir / "logo.png", 512, 512, logo)
    print(f"wrote {out_dir / 'logo.png'} (512x512)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())