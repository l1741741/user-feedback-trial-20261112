#!/usr/bin/env python3
"""Render the dash reference sheet and measure its dash runs.

Run from the repository root, with the same Cairo library path as the tests:

    DYLD_FALLBACK_LIBRARY_PATH=... .venv/bin/python examples/dash-patterns.py

The script writes ``examples/dash-patterns.png`` and prints, for each labelled
row, the painted/blank run lengths measured along the row. The numbers are the
units the SVG asked for, which makes it easy to check a change by eye and to
compare two renderers: both should report the same sequence of runs.

For a second opinion, open ``examples/dash-patterns.svg`` in a browser. The
lines all start at x=340 on the same white background, so the two renderings
can be compared row by row without any reference image.
"""

import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import cairosvg  # noqa: E402
from PIL import Image  # noqa: E402

SOURCE = os.path.join(HERE, 'dash-patterns.svg')
OUTPUT = os.path.join(HERE, 'dash-patterns.png')

# Rows to measure: the label and the y coordinate of the stroked line.
ROWS = (
    ('10 5, phase 0', 16),
    ('5 3 2, phase 0', 48),
    ('5 3 2, offset 11', 80),
    ('5 3 2, offset -20', 112),
    ('10 5, offset -3', 144),
    ('0 12, round caps', 176),
    ('0 12, butt caps', 208),
    ('10% 5%', 240),
    ('1e1 5e0', 272),
    ('4 -2 (invalid)', 304),
)
X0, X1 = 340, 620


def runs(values):
    """Collapse a sequence of booleans into ``[(value, length), ...]``."""
    result = []
    current, count = values[0], 0
    for value in values:
        if value == current:
            count += 1
        else:
            result.append((current, count))
            current, count = value, 1
    result.append((current, count))
    return result


def main():
    with open(SOURCE, 'rb') as source:
        png = cairosvg.svg2png(bytestring=source.read())
    with open(OUTPUT, 'wb') as output:
        output.write(png)

    image = Image.open(io.BytesIO(png)).convert('L')
    pixels = image.load()
    print(f'wrote {OUTPUT} ({image.width}x{image.height})')
    for label, y in ROWS:
        # A 3-unit stroke centered on y covers y-1..y+1; read the middle row.
        values = [pixels[x, y] < 128 for x in range(X0, X1)]
        measured = ', '.join(
            f'{"#" if painted else "."}{length}'
            for painted, length in runs(values))
        print(f'{label:22} y={y:<4} x={X0}..{X1}  {measured}')


if __name__ == '__main__':
    main()
