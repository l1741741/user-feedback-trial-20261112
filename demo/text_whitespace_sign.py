#!/usr/bin/env python3
"""Render an xml:space default/preserve comparison sign to a PNG image.

Two panels show the same source text rendered with the default and the
preserve white-space rules. Each panel displays the text baseline (red
dashed line) and the boundary of the laid-out text (blue rectangle), and
the rendered ink is checked against that boundary with a small
anti-aliasing tolerance.

Run from the repository root (see NATIVE-RUNTIME.md for the cairo library
path):

    .venv/bin/python demo/text_whitespace_sign.py

The image is written next to this script as ``text_whitespace_sign.png``.
"""

import io
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cairocffi as cairo  # noqa: E402

from cairosvg import svg2png  # noqa: E402
from cairosvg.parser import Tree  # noqa: E402

FONT_CANDIDATES = (
    ('Menlo', '/System/Library/Fonts/Menlo.ttc'),
    ('Courier', '/System/Library/Fonts/Courier.ttc'),
    ('DejaVu Sans Mono',
     '/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf'),
    ('Liberation Mono',
     '/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf'),
)
FONT_FAMILY = next(
    (name for name, path in FONT_CANDIDATES if os.path.exists(path)), None)
if FONT_FAMILY is None:
    sys.exit('no known monospace font available on this machine')

WIDTH, HEIGHT = 520, 360
FONT_SIZE = 28
TEXT_X = 30
# The same messy source text is used in both panels: newlines, tabs and
# repeated spaces mixed with tspan elements.
SOURCE_TEXT = 'SIGN  SHOP\n\t<tspan>  No </tspan><tspan></tspan>  319  '
PANELS = (
    ('xml:space default', '', 30, 130),
    ('xml:space="preserve"', 'xml:space="preserve"', 200, 300),
)

OUTPUT = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'text_whitespace_sign.png')


def find_text(node):
    if node.tag == 'text':
        return node
    for child in node.children:
        found = find_text(child)
        if found is not None:
            return found


def flattened_text(node):
    return (node.text or '') + ''.join(
        flattened_text(child) for child in node.children)


def flatten(space_attribute):
    """Flatten the source text with the real CairoSVG parser."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg">'
        f'<text {space_attribute}>{SOURCE_TEXT}</text></svg>')
    return flattened_text(find_text(Tree(bytestring=svg)))


def text_boundary(text, x, y):
    """Return the (x, y, width, height) boundary of the laid-out text."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 1, 1)
    context = cairo.Context(surface)
    context.select_font_face(FONT_FAMILY)
    context.set_font_size(FONT_SIZE)
    x_bearing, y_bearing, width, height = (
        context.text_extents(text)[:4])
    return x + x_bearing, y + y_bearing, width, height


def ink_boundary(png, y_min, y_max):
    """Return the boundary of black ink pixels in a horizontal band."""
    image = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    stride = image.get_stride()
    data = image.get_data()
    x_min, x_max, ink_y_min, ink_y_max = None, None, None, None
    for y in range(y_min, y_max):
        for x in range(image.get_width()):
            offset = y * stride + x * 4
            blue, green, red, alpha = data[offset:offset + 4]
            if red + green + blue < 384:
                x_min = x if x_min is None else min(x_min, x)
                x_max = x if x_max is None else max(x_max, x)
                ink_y_min = y if ink_y_min is None else min(ink_y_min, y)
                ink_y_max = y if ink_y_max is None else max(ink_y_max, y)
    return x_min, ink_y_min, x_max, ink_y_max


def main():
    panels_svg = []
    checks = []
    for label, space_attribute, label_y, baseline_y in PANELS:
        text = flatten(space_attribute)
        box_x, box_y, box_width, box_height = text_boundary(
            text, TEXT_X, baseline_y)
        checks.append((label, text, baseline_y,
                       (box_x, box_y, box_width, box_height)))
        panels_svg.append(f'''
    <text x="{TEXT_X}" y="{label_y}" font-family="{FONT_FAMILY}"
        font-size="14" fill="dimgray"
        xml:space="preserve">{label}: {text}</text>
    <line x1="20" y1="{baseline_y}" x2="{WIDTH - 20}" y2="{baseline_y}"
        stroke="red" stroke-width="1" stroke-dasharray="6 4"/>
    <rect x="{box_x:.2f}" y="{box_y:.2f}"
        width="{box_width:.2f}" height="{box_height:.2f}"
        fill="none" stroke="blue" stroke-width="1"/>
    <text x="{TEXT_X}" y="{baseline_y}" font-family="{FONT_FAMILY}"
        font-size="{FONT_SIZE}">{text}</text>''')

    svg = f'''<svg xmlns="http://www.w3.org/2000/svg"
    width="{WIDTH}" height="{HEIGHT}">
  <rect width="{WIDTH}" height="{HEIGHT}" fill="white"/>
  {''.join(panels_svg)}
</svg>'''

    png = svg2png(bytestring=svg, background_color='white')
    with open(OUTPUT, 'wb') as png_file:
        png_file.write(png)

    # Check the rendered ink against the displayed text boundaries
    for label, text, baseline_y, boundary in checks:
        box_x, box_y, box_width, box_height = boundary
        x_min, y_min, x_max, y_max = ink_boundary(
            png, int(box_y) - 4, int(box_y + box_height) + 5)
        ok = (
            abs(x_min - box_x) <= 2 and abs(x_max - (box_x + box_width)) <= 2
            and abs(y_min - box_y) <= 2
            and abs(y_max - (box_y + box_height)) <= 2)
        status = 'OK ' if ok else 'FAIL'
        print(f'[{status}] {label}: {text!r} ink=({x_min}, {y_min})-'
              f'({x_max}, {y_max}) boundary=({box_x:.1f}, {box_y:.1f}) '
              f'{box_width:.1f}x{box_height:.1f}')
        if not ok:
            sys.exit(1)
    print(f'sign written to {OUTPUT}')


if __name__ == '__main__':
    main()
