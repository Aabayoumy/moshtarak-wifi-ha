#!/usr/bin/env python3
"""Verify the generated brand PNGs actually contain a power symbol.

The project's rule is that a file existing is not the same as a file being
correct - and a stale or malformed icon is exactly the kind of thing that only
shows up later, in someone else's dashboard. So the images are decoded and the
GEOMETRY is asserted, not just the byte count.

Checks:
  1. decodes, at the expected size
  2. the mark is white-on-gradient (white pixel fraction in a sane band)
  3. the ring exists at left and right of centre
  4. the ring is OPEN at the top - the 90 degree gap is the whole point of the
     IEC 60417 power symbol, and a closed ring would just be a circle
  5. the vertical bar runs from above the ring down through the centre
  6. the corners are background, so the gradient covers the tile
"""
from __future__ import annotations

import math
import struct
import sys
import zlib
from pathlib import Path


def decode_png(path: Path) -> tuple[int, int, list[list[tuple[int, int, int]]]]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n", f"{path}: not a PNG"

    pos = 8
    width = height = 0
    idat = bytearray()
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos:pos + 4])
        tag = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            width, height, depth, colour = struct.unpack(">IIBB", body[:10])
            assert depth == 8 and colour == 2, f"{path}: expected 8-bit RGB"
        elif tag == b"IDAT":
            idat += body
        elif tag == b"IEND":
            break
        pos += 12 + length

    raw = zlib.decompress(bytes(idat))
    stride = width * 3
    rows: list[list[tuple[int, int, int]]] = []
    prev = bytearray(stride)
    p = 0
    for _ in range(height):
        ftype = raw[p]
        p += 1
        line = bytearray(raw[p:p + stride])
        p += stride
        if ftype == 0:
            pass
        elif ftype == 1:  # Sub
            for i in range(3, stride):
                line[i] = (line[i] + line[i - 3]) & 0xFF
        elif ftype == 2:  # Up
            for i in range(stride):
                line[i] = (line[i] + prev[i]) & 0xFF
        elif ftype == 4:  # Paeth
            for i in range(stride):
                a = line[i - 3] if i >= 3 else 0
                b = prev[i]
                c = prev[i - 3] if i >= 3 else 0
                pa, pb, pc = abs(b - c), abs(a - c), abs(a + b - 2 * c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        else:
            raise AssertionError(f"{path}: unsupported filter {ftype}")
        rows.append([tuple(line[x:x + 3]) for x in range(0, stride, 3)])
        prev = line
    return width, height, rows


def is_white(px: tuple[int, int, int], tol: int = 40) -> bool:
    return all(abs(c - 255) <= tol for c in px)


def check(path: Path, expect_size: int) -> bool:
    w, h, rows = decode_png(path)
    name = path.name
    fails: list[str] = []

    def ok(cond: bool, msg: str) -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {msg}")
        if not cond:
            fails.append(msg)

    print(f"\n{name} ({w}x{h})")
    ok(w == h == expect_size, f"decodes at {expect_size}x{expect_size}")

    total_white = sum(
        1 for row in rows for px in row if is_white(px)
    )
    frac = total_white / (w * h)
    ok(0.05 <= frac <= 0.30, f"white coverage is plausible ({frac:.1%})")

    cx = cy = w / 2
    radius = h * 0.26
    thickness = h * 0.085
    band = thickness / 2 - 4  # sample inside the ring, away from the edge

    def px_at(radius_offset: float, angle_deg: float) -> tuple[int, int, int]:
        # angle 0 = straight up, clockwise positive
        a = math.radians(angle_deg)
        fx = cx + math.sin(a) * (radius + radius_offset)
        fy = cy - math.cos(a) * (radius + radius_offset)
        return rows[int(fy)][int(fx)]

    ok(is_white(px_at(-band, 90)), "ring present at the right of centre (+90 deg)")
    ok(is_white(px_at(-band, -90)), "ring present at the left of centre (-90 deg)")
    ok(is_white(px_at(-band, 150)), "ring present at lower right (+150 deg)")
    ok(is_white(px_at(-band, -150)), "ring present at lower left (-150 deg)")

    # The gap. The IEC 60417 power symbol is a ring with a 90 degree gap at the
    # top, crossed by the vertical bar. So the gap region is background
    # EVERYWHERE EXCEPT where the bar crosses it - sampling straight up is
    # supposed to be white, and an earlier version of this check asserted the
    # opposite and failed on a correct image.
    for angle in (-40, -25, 25, 40):
        ok(not is_white(px_at(-band, angle)), f"ring is open at {angle:+d} deg")

    # At the very top the only white should be the bar, and it should be narrow.
    top_y = int(cy - radius)
    run = [x for x in range(w) if is_white(rows[top_y][x])]
    ok(
        len(run) > 0 and abs((min(run) + max(run)) / 2 - cx) < thickness,
        "the bar sits on the vertical centre line",
    )
    ok(
        len(run) <= thickness * 2.5,
        f"only the bar crosses the gap, not a closed ring ({len(run)}px wide)",
    )

    # The bar: vertical, through the centre, from above the ring down to it.
    bar_top = cy - radius - thickness * 0.35
    for frac in (0.05, 0.35, 0.65, 0.9):
        y = bar_top + (cy - bar_top) * frac
        ok(is_white(rows[int(y)][int(cx)]), f"bar is white at {frac:.0%} down the bar")

    # Corners must be background, so the gradient covers the tile.
    corners = [rows[1][1], rows[1][w - 2], rows[h - 2][1], rows[h - 2][w - 2]]
    ok(all(not is_white(c) for c in corners), "corners are background, not the mark")

    return not fails


def main() -> int:
    base = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(".")
    results = [check(base / "icon.png", 256), check(base / "logo.png", 512)]
    print()
    if all(results):
        print("ALL BRAND CHECKS PASSED")
        return 0
    print("BRAND CHECKS FAILED")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())