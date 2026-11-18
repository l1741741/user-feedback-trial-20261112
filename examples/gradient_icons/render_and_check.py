#!/usr/bin/env python3
"""Offline demo and pixel-level check for shared-gradient brand icons.

This script renders the two side-by-side icon sheets that differ only by the
order of their icon nodes and verifies, by sampling the actual PNG pixels,
that:

1. Both sheets are byte-for-byte identical (drawing order has no effect).
2. Each icon keeps its own color distribution:
     - icon 1 inherits the public blue -> red horizontal gradient;
     - icon 2 keeps its own green -> yellow stops on a vertical axis;
     - icon 3 is a translucent circle inheriting the public gradient;
     - icon 4 is a transformed radial gradient with a bright band;
     - icon 5 is the leaf of a 3-level chain with its own green -> yellow
       stops;
     - icon 6 paints the chain's intermediate gradient directly and must
       keep its inherited blue -> red vertical gradient even though icon 5
       resolved the same chain first.

Run it with the project virtualenv, giving the paths explicitly so no
import is ever loaded from the current working directory:

    DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib" \\
        ../../.venv/bin/python -I render_and_check.py

The two rendered PNGs are written next to the SVGs.
"""

import io
import pathlib
import sys

import cairocffi as cairo
import cairosvg

HERE = pathlib.Path(__file__).resolve().parent
ORDER_1 = HERE / 'brand-icons-order-1.svg'
ORDER_2 = HERE / 'brand-icons-order-2.svg'
PNG_1 = HERE / 'brand-icons-order-1.png'
PNG_2 = HERE / 'brand-icons-order-2.png'

WIDTH, HEIGHT = 384, 64
ICON_SLOT = 64


def render(svg_path):
    """Render an SVG file to PNG bytes and return (bytes, ImageSurface)."""
    png_bytes = cairosvg.svg2png(
        url=str(svg_path), output_width=WIDTH, output_height=HEIGHT)
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))
    return png_bytes, surface


def rgba(surface, x, y):
    """Return (r, g, b, a); cairocffi stores little-endian ARGB = BGRA."""
    data = surface.get_data()
    offset = y * surface.get_stride() + x * 4
    blue, green, red, alpha = data[offset:offset + 4]
    return red, green, blue, alpha


def check(condition, message):
    print(('  PASS ' if condition else '  FAIL ') + message)
    return condition


def main():
    ok = True
    print('Rendering both icon sheets...')
    png_1, surface_1 = render(ORDER_1)
    png_2, surface_2 = render(ORDER_2)
    PNG_1.write_bytes(png_1)
    PNG_2.write_bytes(png_2)
    print(f'  wrote {PNG_1.name} and {PNG_2.name}')

    print('\n1) Drawing order must not change the result:')
    ok &= check(
        png_1 == png_2,
        'both orders produce identical PNG bytes')

    print('\n2) Per-icon color distribution (sampled from order #1):')
    # Icon 1: blue on the left, red on the right, fully opaque.
    left = rgba(surface_1, 8, 32)
    right = rgba(surface_1, 56, 32)
    ok &= check(left[2] > 150 and left[0] < 120,
                f'icon 1 left edge is blue-ish {left}')
    ok &= check(right[0] > 150 and right[2] < 120,
                f'icon 1 right edge is red-ish {right}')

    # Icon 2: green at the top, yellow at the bottom (own stops, vertical).
    top = rgba(surface_1, 64 + 32, 8)
    bottom = rgba(surface_1, 64 + 32, 56)
    ok &= check(top[1] > 150 and top[0] < 120,
                f'icon 2 top is green-ish {top}')
    ok &= check(bottom[0] > 150 and bottom[1] > 150 and bottom[2] < 120,
                f'icon 2 bottom is yellow-ish {bottom}')

    # Icon 3: translucent circle (alpha well below the opaque icons).
    center = rgba(surface_1, 128 + 32, 32)
    ok &= check(0 < center[3] < 255,
                f'icon 3 center is translucent (alpha={center[3]})')

    # Icon 4: transformed radial gradient. The horizontal squash moves the
    # inherited bright (white) stop to a band on the left while the far side
    # stays at the inherited dark stop.
    bright_band = rgba(surface_1, 192 + 16, 32)   # x ~ 208
    dark_region = rgba(surface_1, 192 + 48, 32)   # x ~ 240
    ok &= check(sum(bright_band[:3]) > sum(dark_region[:3]),
                f'icon 4 bright band {bright_band} brighter than dark region '
                f'{dark_region}')

    # Icon 5: chain leaf keeps its own green -> yellow stops.
    leaf_top = rgba(surface_1, 256 + 32, 8)
    leaf_bottom = rgba(surface_1, 256 + 32, 56)
    ok &= check(leaf_top[1] > 150 and leaf_top[0] < 120,
                f'icon 5 top is green-ish {leaf_top}')
    ok &= check(leaf_bottom[0] > 150 and leaf_bottom[1] > 150,
                f'icon 5 bottom is yellow-ish {leaf_bottom}')

    # Icon 6: the chain's intermediate gradient painted directly must keep
    # its inherited blue -> red vertical gradient and stay fully opaque.
    # On the buggy implementation this rect was black/transparent because
    # resolving icon 5 first had mutated the shared mid definition.
    mid_top = rgba(surface_1, 320 + 32, 8)
    mid_bottom = rgba(surface_1, 320 + 32, 56)
    ok &= check(mid_top[2] > 150 and mid_top[0] < 120 and mid_top[3] == 255,
                f'icon 6 top is opaque blue-ish {mid_top}')
    ok &= check(
        mid_bottom[0] > 150 and mid_bottom[2] < 120 and mid_bottom[3] == 255,
        f'icon 6 bottom is opaque red-ish {mid_bottom}')

    print('\n3) Each icon must match across the two orders:')
    for slot in range(6):
        x = slot * ICON_SLOT + ICON_SLOT // 2
        samples_1 = [rgba(surface_1, x, y) for y in (8, 24, 40, 56)]
        samples_2 = [rgba(surface_2, x, y) for y in (8, 24, 40, 56)]
        ok &= check(samples_1 == samples_2,
                    f'icon slot {slot} identical in both orders')

    print('\nRESULT: ' + ('all checks passed' if ok else 'CHECKS FAILED'))
    return 0 if ok else 1


if __name__ == '__main__':
    sys.exit(main())
