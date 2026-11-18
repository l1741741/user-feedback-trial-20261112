#!/usr/bin/env python
"""Render ``filter_offset_demo.svg`` and verify it with exact pixel probes.

The demo is offline and needs no network: it uses only the CairoSVG package
next to it. Run it from the repository root:

    .venv/bin/python demo_filters/render_demo.py

It writes ``filter_offset_demo.png`` next to the fixture and checks the
position of the source, the semi-transparent shadow, the transparent areas
and the region-clipped offset. It exits with a non-zero status on the first
mismatch, so it doubles as a smoke test.

"""

import io
import sys
from pathlib import Path

import cairosvg
from PIL import Image

HERE = Path(__file__).resolve().parent
FIXTURE = HERE / 'filter_offset_demo.svg'
OUTPUT = HERE / 'filter_offset_demo.png'

RED = (204, 34, 34, 255)          # source fill, #cc2222
BLACK = (0, 0, 0, 255)
GREEN = (35, 136, 35, 255)        # clipped source fill, #238823
LIME = (136, 204, 34, 255)        # unfiltered sibling, #88cc22
FRAME = (34, 34, 34, 255)         # border stroke, #222222
SHADOW_HALF_BLACK = (0, 0, 0, 128)


def render():
    """Render the fixture to a PIL image and write the PNG next to it."""
    png = cairosvg.svg2png(url=str(FIXTURE))
    OUTPUT.write_bytes(png)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def check_pixel(image, x, y, expected, label):
    """Assert the RGBA value of one pixel, allowing one unit of rounding."""
    actual = image.getpixel((x, y))
    if actual[3] == 0 and expected[3] == 0:
        # Fully transparent pixels carry no meaningful color.
        return
    if len(expected) == 4 and all(
            abs(actual[index] - expected[index]) <= 1 for index in range(4)):
        return
    raise AssertionError(
        f'{label}: pixel ({x}, {y}) is {actual}, expected {expected}')


def check_clear(image, points, label):
    """Assert that pixels are fully transparent."""
    for x, y in points:
        actual = image.getpixel((x, y))
        if actual[3] != 0:
            raise AssertionError(
                f'{label}: pixel ({x}, {y}) is {actual}, expected clear')


def main():
    """Render the demo, verify it and print a short report."""
    image = render()
    if image.size != (240, 180):
        raise AssertionError(f'Unexpected output size {image.size}')

    # 1. The unfiltered frame is exactly where it is drawn: a 2-unit stroke
    # centred on x = 4/236 and y = 4/176.
    check_pixel(image, 4, 90, FRAME, 'frame left edge')
    check_pixel(image, 236, 90, FRAME, 'frame right edge')
    check_pixel(image, 120, 4, FRAME, 'frame top edge')

    # 2. The source rectangle keeps its opaque red at its original position
    # (40, 30) 60x30, and the shadow is (24, 12) away from it.
    check_pixel(image, 70, 45, RED, 'source rectangle')
    check_pixel(image, 64, 36, RED, 'source top left')
    check_pixel(image, 110, 50, SHADOW_HALF_BLACK, 'shadow right of source')
    check_pixel(image, 70, 65, SHADOW_HALF_BLACK, 'shadow below source')
    check_pixel(image, 120, 70, SHADOW_HALF_BLACK, 'shadow bottom right')

    # 3. Pixels left clear by the offset stay fully transparent.
    check_clear(image, [(126, 75), (30, 20)], 'shadow gaps')

    # 4. The clipped offset paints only the part inside the region: the
    # source box is (40, 100) 40x20 and the region is exactly that box, so
    # of the (16, 8) shift only the (56, 108) 24x12 overlap is painted.
    check_pixel(image, 60, 112, GREEN, 'clipped offset visible part')
    check_clear(image, [(44, 102), (44, 116), (82, 112)], 'outside region')

    # 5. The unfiltered sibling is untouched.
    check_pixel(image, 200, 140, LIME, 'unfiltered sibling')

    print(f'Wrote {OUTPUT}')
    print('All demo pixel checks passed.')


if __name__ == '__main__':
    try:
        main()
    except AssertionError as error:
        print(f'Demo check failed: {error}', file=sys.stderr)
        sys.exit(1)
