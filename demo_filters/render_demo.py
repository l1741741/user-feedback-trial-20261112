#!/usr/bin/env python3
"""
Offline demo for SVG filter offsets and filter regions.

Renders ``filter_offset_demo.svg`` next to this script with CairoSVG and
checks the resulting PNG pixel by pixel. No network access is needed.

Run it from the repository root::

    export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib"  # macOS, if needed
    .venv/bin/python demo_filters/render_demo.py

It writes ``filter_offset_demo.png`` in this directory and exits with a
non-zero status if a probe does not match the declared geometry.

"""

import io
import sys
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import cairosvg  # noqa: E402

CLEAR = (0, 0, 0, 0)
RED = (255, 0, 0, 255)
GREEN = (0, 128, 0, 255)
LIME = (0, 255, 0, 255)
SHADOW = (0, 0, 0, 128)

# The fixture, stated again so the checks read as a specification:
#
# * a black frame, x=4 y=4, 232x172, unfiltered;
# * a red 60x30 rectangle at (40, 30) with a 50%-opaque black drop shadow
#   offset by (24, 12): the shadow spans (64, 42) to (124, 72), the source
#   keeps (40, 30) to (100, 60);
# * a green 40x20 rectangle at (40, 100) with a filter region reduced to the
#   source box and an offset of (16, 8): the result is clipped to
#   (40, 100)-(80, 120), leaving (56, 108)-(80, 120) visible;
# * an unfiltered lime 20x20 square at (190, 130), which must not move.
PROBES = [
    # Source graphic: opaque red, untouched by the filter.
    ((50, 40), RED),
    ((99, 59), RED),
    # Semi-transparent shadow, only outside the source graphic.
    ((86, 60), SHADOW),
    ((110, 45), SHADOW),
    # Transparent pixels where only the offset moved the graphic away.
    ((40, 65), CLEAR),
    ((110, 75), CLEAR),
    # Green layer: visible where the offset stays inside the region.
    ((60, 110), GREEN),
    ((79, 119), GREEN),
    # Clipped by the region on the right and bottom edges.
    ((80, 110), CLEAR),
    ((60, 120), CLEAR),
    # Transparent area left behind by the offset.
    ((41, 101), CLEAR),
    # Unfiltered sibling: exact position and color.
    ((190, 130), LIME),
    ((209, 149), LIME),
    ((189, 129), CLEAR),
    # Frame, drawn by an unfiltered sibling (2px stroke on x=4): the center
    # of the band is black, just inside it is transparent.
    ((4, 4), (0, 0, 0, 255)),
    ((2, 4), CLEAR),
    ((6, 6), CLEAR),
]


def main():
    """Render the demo, write the PNG and check every probe."""
    source = (HERE / 'filter_offset_demo.svg').read_bytes()
    png = cairosvg.svg2png(bytestring=source)
    output = HERE / 'filter_offset_demo.png'
    output.write_bytes(png)

    image = Image.open(io.BytesIO(png)).convert('RGBA')
    failures = []
    for (x, y), expected in PROBES:
        actual = image.getpixel((x, y))
        status = 'ok' if actual == expected else 'FAIL'
        print(f'  ({x:3d}, {y:3d}) {actual} expected {expected} {status}')
        if actual != expected:
            failures.append((x, y, actual, expected))

    print(f'\nWrote {output} ({image.width}x{image.height})')
    print(f'{len(PROBES) - len(failures)}/{len(PROBES)} probes passed')
    if failures:
        for failure in failures:
            print('  mismatch:', failure)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
