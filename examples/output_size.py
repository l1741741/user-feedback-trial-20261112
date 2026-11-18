#!/usr/bin/env python
"""Runnable example: control the size of a PNG produced by CairoSVG.

Run it from the repository root with the interpreter that has CairoSVG
installed, for example::

    python examples/output_size.py

It renders the same small SVG with several sizing options, checks the pixel
size of each produced PNG and verifies that the image edges are fully
painted (no transparent line), then writes the files to a temporary directory
and prints a summary.

The example uses only the public :func:`cairosvg.svg2png` API. See
``cairosvg/test_output_viewport.py`` for the corresponding regression tests.
"""

import io
import tempfile
from pathlib import Path

from PIL import Image

import cairosvg

# A 20x20 unit square with a non-zero viewBox origin:
#   - four coloured corner marks,
#   - a cross through the center,
#   - a red background covering the whole viewport.
#
# The corner marks make it easy to check at a glance that the content is
# laid out for the whole image and not for a smaller, fractional viewport.
SVG = b'''\
<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"
     viewBox="7 3 20 20">
  <rect x="7" y="3" width="20" height="20" fill="#c00000"/>
  <rect x="7" y="3" width="4" height="4" fill="#00c000"/>
  <rect x="23" y="3" width="4" height="4" fill="#0000c0"/>
  <rect x="7" y="19" width="4" height="4" fill="#c0c000"/>
  <rect x="23" y="19" width="4" height="4" fill="#00c0c0"/>
  <rect x="16" y="3" width="2" height="20" fill="#101010"/>
  <rect x="7" y="12" width="20" height="2" fill="#101010"/>
</svg>'''

#: ``(label, options, expected PNG size, fully painted edges)``.
CASES = (
    ('default (source size)', {}, (20, 20), True),
    ('output_width=25.5', {'output_width': 25.5}, (26, 26), True),
    ('output_width=37.8', {'output_width': 37.8}, (38, 38), True),
    ('output_height=25.5', {'output_height': 25.5}, (26, 26), True),
    ('scale=2.71', {'scale': 2.71}, (54, 54), True),
    ('dpi=150', {'dpi': 150}, (20, 20), True),
    ('output_width=40, output_height=40',
     {'output_width': 40, 'output_height': 40}, (40, 40), True),
    # Both dimensions are used literally; a non-matching aspect ratio
    # letterboxes the content (transparent bands) and the file stays 40x20.
    ('output_width=40, output_height=20',
     {'output_width': 40, 'output_height': 20}, (40, 20), False),
)


def border_pixels(image):
    """Yield the pixels of the four edges of ``image``."""
    width, height = image.size
    for x in range(width):
        yield image.getpixel((x, 0))
        yield image.getpixel((x, height - 1))
    for y in range(height):
        yield image.getpixel((0, y))
        yield image.getpixel((width - 1, y))


def main(output_directory=None):
    """Render every case and check its size and its edges."""
    directory = Path(output_directory or tempfile.mkdtemp())
    directory.mkdir(parents=True, exist_ok=True)
    succeeded = True

    for index, (label, options, expected_size, painted) in enumerate(CASES):
        png = cairosvg.svg2png(SVG, **options)
        image = Image.open(io.BytesIO(png)).convert('RGBA')
        size_ok = image.size == expected_size
        # When the content is expected to cover the whole viewport, every
        # edge pixel must be fully opaque: a partially transparent edge is
        # the transparent line left by a fractional viewport.
        edges_opaque = all(pixel[3] == 255 for pixel in border_pixels(image))
        edges_ok = edges_opaque if painted else True

        suffix = '-'.join(f'{key}_{value}' for key, value in options.items())
        path = directory / f'{index}-{suffix or "default"}.png'
        path.write_bytes(png)

        status = 'ok' if size_ok and edges_ok else 'FAILED'
        succeeded &= status == 'ok'
        print(
            f'{label:38s} size={image.size!s:10s} '
            f'expected={expected_size!s:10s} edges opaque={edges_opaque} '
            f'-> {status} ({path})')

    # Invalid sizes are rejected with the documented ValueError instead of
    # producing a successful-looking empty file.
    for bad_width in (0, -1, float('nan'), float('inf')):
        try:
            cairosvg.svg2png(SVG, output_width=bad_width)
        except ValueError:
            print(f'output_width={bad_width!r:6s} '
                  f'-> rejected with ValueError (expected)')
        else:
            succeeded = False
            print(f'output_width={bad_width!r} -> accepted (unexpected!)')

    print('All checks passed.' if succeeded else 'Some checks failed.')
    return succeeded


if __name__ == '__main__':
    import sys

    sys.exit(0 if main() else 1)
