#!/usr/bin/env python3
"""Standalone visual example for stroke dashing.

The script builds an SVG "ruler" where every stroke sits on a numbered
10-unit reference scale, so the start and end of each inked dash and gap can
be read directly from the ticks. One user unit is one exported pixel. It
writes the SVG and a PNG next to each other and prints, for every row, the
measured ink runs in pixels, which are also the user-unit positions.

It is fully self-contained. With the project's virtualenv and a Cairo shared
library available, run for example::

    DYLD_FALLBACK_LIBRARY_PATH=/path/to/cairo/lib \\
        python examples/dash_stroke_demo.py dash_stroke_demo

This writes ``dash_stroke_demo.svg`` and ``dash_stroke_demo.png``.

The rows demonstrate, from top to bottom:

1. a plain even dash list;
2. an odd list, equivalent to its SVG-doubled form;
3. a negative ``stroke-dashoffset`` shifting the phase forward;
4. an invalid declaration (a negative length), painted solid;
5. an all-zero array, which disables the stroke;
6. two subpaths, each restarting the phase at its own start point;
7. a closed polygon whose pattern continues around every corner.

"""

import io
import sys

import cairocffi as cairo

from cairosvg import svg2png

ROW_H = 26
LEFT = 30          # first user-unit coordinate carrying a stroke
RIGHT = 480
WIDTH = 520
HEIGHT = 7 * ROW_H + 24


def ruler_row(y, dasharray=None, dashoffset=None, closed=False):
    """Return the SVG markup for one dashed row on the reference ruler."""
    parts = [f'<g transform="translate(0,{y})">']
    # Ticks hang below the stroke row so they never overlap the measured ink.
    for unit in range(LEFT, RIGHT + 1):
        if unit % 50 == 0:
            length, shade, width = 12, '#777', 1.4
        elif unit % 10 == 0:
            length, shade, width = 7, '#999', 1.0
        elif unit % 5 == 0:
            length, shade, width = 4, '#ccc', 0.8
        else:
            continue
        parts.append(
            f'<line x1="{unit}" y1="3" x2="{unit}" y2="{3 + length}" '
            f'stroke="{shade}" stroke-width="{width}"/>')
    attrs = ['stroke="#0b5fff"', 'stroke-width="4"', 'fill="none"']
    if dasharray is not None:
        attrs.append(f'stroke-dasharray="{dasharray}"')
    if dashoffset is not None:
        attrs.append(f'stroke-dashoffset="{dashoffset}"')
    if closed:
        # A shallow step kept inside this row's band so it never overlaps the
        # neighbouring rows.
        parts.append(
            f'<path d="M{LEFT} 0 L{RIGHT - 80} 0 L{RIGHT - 80} -14 '
            f'L{RIGHT} -14" {" ".join(attrs)}/>')
    else:
        parts.append(
            f'<line x1="{LEFT}" y1="0" x2="{RIGHT}" y2="0" '
            f'{" ".join(attrs)}/>')
    parts.append('</g>')
    return ''.join(parts)


def build_svg():
    subpaths_y = 18 + 5 * ROW_H
    rows = [
        ruler_row(18, dasharray='40,16'),
        ruler_row(18 + ROW_H, dasharray='40,16,8'),
        ruler_row(18 + 2 * ROW_H, dasharray='40,16', dashoffset='-12'),
        ruler_row(18 + 3 * ROW_H, dasharray='20,-8'),
        ruler_row(18 + 4 * ROW_H, dasharray='0,0'),
        # Two independent subpaths in one path element.
        (f'<g transform="translate(0,{subpaths_y})">'
         f'<path d="M{LEFT} 0 L220 0 M260 0 L{RIGHT} 0" stroke="#0b5fff" '
         f'stroke-width="4" stroke-dasharray="40,16"/></g>'),
        ruler_row(18 + 6 * ROW_H, dasharray='48,16', closed=True),
    ]
    labels = [
        (4, 18, 'even list "40,16"'),
        (4, 18 + ROW_H, 'odd list "40,16,8"'),
        (4, 18 + 2 * ROW_H, 'offset -12'),
        (4, 18 + 3 * ROW_H, 'invalid "20,-8" -> solid'),
        (4, 18 + 4 * ROW_H, 'all zero -> no stroke'),
        (4, subpaths_y, 'two subpaths, phase restarts'),
        (4, 18 + 6 * ROW_H, 'closed path, phase around corners'),
    ]
    text = ''.join(
        f'<text x="{x}" y="{y - 6}" font-family="monospace" font-size="11" '
        f'fill="#222">{label}</text>' for x, y, label in labels)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}">' + text + ''.join(rows) + '</svg>')
    return svg


def measure(png_bytes):
    """Print measured ink runs for the straight rows of the exported PNG.

    Only the blue stroke color is counted, so gray ruler ticks and black text
    never contribute to the runs.

    """
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))
    data = bytes(surface.get_data())
    stride = surface.get_stride()

    def blue_at(x, y):
        offset = y * stride + x * 4
        # Premultiplied ARGB little-endian storage: B, G, R, A.
        blue, green, red, alpha = data[offset:offset + 4]
        return alpha > 0 and blue > red + 40 and blue > green + 20

    def runs(y):
        found, previous, start = [], False, 0
        for x in range(surface.get_width()):
            inked = blue_at(x, y)
            if inked and not previous:
                start = x
            elif not inked and previous:
                found.append((start, x - start))
            previous = inked
        if previous:
            found.append((start, surface.get_width() - start))
        return found

    for label, y in (
            ('even list        ', 18),
            ('odd list         ', 18 + ROW_H),
            ('offset -12       ', 18 + 2 * ROW_H),
            ('invalid -> solid ', 18 + 3 * ROW_H),
            ('all zero         ', 18 + 4 * ROW_H),
            ('subpaths         ', 18 + 5 * ROW_H)):
        measured = runs(y)
        print(f'{label}: {measured[:6]}'
              + (' ...' if len(measured) > 6 else ''))


def main(prefix='dash_stroke_demo'):
    svg = build_svg()
    png = svg2png(bytestring=svg.encode())
    with open(f'{prefix}.svg', 'w', encoding='utf-8') as svg_file:
        svg_file.write(svg)
    with open(f'{prefix}.png', 'wb') as png_file:
        png_file.write(png)
    print(f'wrote {prefix}.svg and {prefix}.png ({len(png)} PNG bytes)')
    measure(png)


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'dash_stroke_demo')
