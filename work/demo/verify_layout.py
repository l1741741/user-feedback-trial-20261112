#!/usr/bin/env python3
"""Pixel-level smoke check for the offline textPath demo.

Renders the demo SVG and inspects regions of the resulting PNG with Pillow:
colored glyph ink must appear where the labels are expected, and the
zero-length path region must contain no red text. This complements the
numeric pytest suite with an end-to-end raster check while keeping a generous
tolerance for font hinting.

Run with the project virtualenv, e.g.::

    .venv/bin/python -I work/demo/verify_layout.py
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

import cairosvg  # noqa: E402
from PIL import Image  # noqa: E402

HERE = Path(__file__).parent
SVG_PATH = HERE / 'text_path_demo.svg'
PNG_PATH = HERE / 'text_path_demo.png'

# The demo is rendered at 2x its 760x1180 user-unit viewport.
SCALE = 2


def ink_count(image, box, predicate):
    """Count pixels in ``box`` (user-unit coordinates) matching ``predicate``."""
    x0, y0, x1, y1 = (int(round(v * SCALE)) for v in box)
    count = 0
    for y in range(y0, y1):
        for x in range(x0, x1):
            r, g, b, a = image.getpixel((x, y))
            if a and predicate(r, g, b):
                count += 1
    return count


def is_colored(r, g, b):
    """A saturated (non-gray) pixel: one channel clearly dominates."""
    return max(r, g, b) - min(r, g, b) > 60


def is_red(r, g, b):
    return r > 120 and g < 90 and b < 90


def main():
    cairosvg.svg2png(
        url=str(SVG_PATH), write_to=str(PNG_PATH),
        output_width=1520, output_height=2360)
    image = Image.open(PNG_PATH).convert('RGBA')

    checks = []

    # 1. Green START label near the left end of the arc (~user x 60-110,
    #    y 180-260).
    checks.append((
        'arc START (green) present',
        ink_count(image, (55, 175, 115, 265),
                  lambda r, g, b: g > 120 and r < 120) > 40))

    # 2. Blue centered label around the arc top midpoint (~x 150-330, y 90-140).
    checks.append((
        'arc centered label (blue) present',
        ink_count(image, (150, 90, 335, 145),
                  lambda r, g, b: b > 120 and r < 120) > 40))

    # 3. Red END label near the right arc end (~x 380-420, y 200-260).
    checks.append((
        'arc END (red) present',
        ink_count(image, (375, 195, 425, 265), is_red) > 20))

    # 4. Purple reversed-polyline text somewhere in its band.
    checks.append((
        'reversed polyline label present',
        ink_count(image, (300, 350, 640, 430),
                  lambda r, g, b: r > 100 and b > 100 and g < 120) > 40))

    # 5. Green subpath text on both subpath lines.
    checks.append((
        'first subpath text present',
        ink_count(image, (35, 515, 205, 545),
                  lambda r, g, b: g > 110 and r < 100) > 30))
    checks.append((
        'second subpath text present',
        ink_count(image, (355, 515, 470, 545),
                  lambda r, g, b: g > 110 and r < 100) > 20))

    # 6. The gap between subpaths (x 200-360, y 515-545) holds no green ink.
    checks.append((
        'no green ink in the subpath gap',
        ink_count(image, (205, 515, 350, 545),
                  lambda r, g, b: g > 110 and r < 100) == 0))

    # 7. Orange closed-triangle text present along a side.
    checks.append((
        'closed triangle label present',
        ink_count(image, (35, 630, 290, 810),
                  lambda r, g, b: r > 150 and 80 < g < 170 and b < 80) > 40))

    # 8. Zero-length path region: no red text anywhere under its heading.
    checks.append((
        'no red text for zero-length path',
        ink_count(image, (35, 900, 400, 950), is_red) == 0))

    # 9. Short path sibling text (blue) on its own lower line.
    checks.append((
        'sibling ordinary text present after short path',
        ink_count(image, (35, 1020, 460, 1050),
                  lambda r, g, b: b > 120 and r < 120) > 40))

    # 10. Both repeated-reference labels present and distinct colors.
    checks.append((
        'first reference (green) present',
        ink_count(image, (35, 1110, 300, 1140),
                  lambda r, g, b: g > 110 and r < 100) > 20))
    checks.append((
        'second reference (red) present',
        ink_count(image, (35, 1140, 320, 1175), is_red) > 20))

    failed = [name for name, ok in checks if not ok]
    width = max(len(name) for name, _ in checks)
    for name, ok in checks:
        print(f"{'PASS' if ok else 'FAIL'}  {name.ljust(width)}")
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
