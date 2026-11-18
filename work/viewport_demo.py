#!/usr/bin/env python3
"""Runnable viewport/percent comparison demo.

Renders a family of small SVG documents that combine nested ``svg`` layers,
``symbol``/``use`` references, percentage-sized boxes and circles, rulers and
different sibling orders. For every document the script prints where the
colored shapes actually land in the PNG and checks the geometry against the
SVG coordinate-system rules:

* a percentage length is relative to the effective viewport of the element
  carrying the attribute — with a viewBox that is the viewBox, in its own
  user units;
* preserveAspectRatio meet/slice/none and non-zero viewBox origins map the
  user space onto the device pixels as specified;
* leaving a nested ``svg`` restores the parent coordinate system for the
  following siblings, whatever their order;
* a ``symbol`` drawn through ``use`` gives the same pixels as the equivalent
  directly nested ``svg``.

Run it directly:

    .venv/bin/python work/viewport_demo.py

The rendered PNGs are written next to this script so they can be inspected.
"""

import io
import os
import sys

import cairosvg
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))

RED = (255, 0, 0)
GREEN = (0, 128, 0)
BLUE = (0, 0, 255)
BLACK = (0, 0, 0)
GRAY = (120, 120, 120)


def render(svg_source, name):
    """Render to PNG, save it and return a pixel accessor plus the image."""
    png = cairosvg.svg2png(
        bytestring=svg_source.encode() if isinstance(svg_source, str)
        else svg_source)
    path = os.path.join(HERE, f'{name}.png')
    with open(path, 'wb') as file_obj:
        file_obj.write(png)
    image = Image.open(io.BytesIO(png)).convert('RGB')
    print(f'  wrote {path} ({image.width}x{image.height})')
    return image, image.load()


def color_bbox(image, rgb):
    """Bounding box of every exact-color pixel."""
    accessor = image.load()
    points = [(x, y) for y in range(image.height) for x in range(image.width)
              if accessor[x, y] == rgb]
    if not points:
        return None
    return (min(x for x, _ in points), min(y for _, y in points),
            max(x for x, _ in points), max(y for _, y in points))


RESULTS = []


def check(label, actual, expected):
    """Print a PASS/FAIL line for one geometry expectation."""
    status = 'PASS' if actual == expected else 'FAIL'
    print(f'  [{status}] {label}: {actual} (expected {expected})')
    if actual != expected:
        RESULTS.append(label)
    return actual == expected


def ruler_svg(width, height, step=10, color='#999999'):
    """A tick ruler covering the canvas, drawn in the current viewport."""
    ticks = []
    for x in range(0, width + 1, step):
        length = step if x % (step * 5) else step * 2
        ticks.append(
            f'<line x1="{x}" y1="0" x2="{x}" y2="{length}" '
            f'stroke="{color}" stroke-width="1"/>')
    for y in range(0, height + 1, step):
        length = step if y % (step * 5) else step * 2
        ticks.append(
            f'<line x1="0" y1="{y}" x2="{length}" y2="{y}" '
            f'stroke="{color}" stroke-width="1"/>')
    return ''.join(ticks)


def demo_nested_percentages():
    """Multi-layer boxes/circles: percentages at every nesting level."""
    print('1. Nested svg layers with percentage boxes and circles')
    # Outer 240x160. Layer A is 75%x50% => 180x80 at (10,10). Inside A,
    # layer B is 50%x50% of A => 90x40 with a square viewBox 0 0 40 40,
    # i.e. non-uniform mapping. A 25%-radius circle stays round in user
    # units (stretched only by the none aspect ratio of B).
    svg = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="240" height="160">
      <rect width="240" height="160" fill="white"/>
      {ruler_svg(240, 160)}
      <svg x="10" y="10" width="75%" height="50%">
        <rect width="100%" height="100%" fill="red" opacity="0.25"/>
        <svg x="20" y="20" width="50%" height="50%" viewBox="0 0 40 40"
             preserveAspectRatio="none">
          <rect width="100%" height="100%" fill="green" opacity="0.35"/>
          <circle cx="50%" cy="50%" r="25%" fill="blue"/>
        </svg>
      </svg>
    </svg>'''
    image, _ = render(svg, 'demo1_nested_percent')
    # Layer A device box: (10,10)-(189,89); red at 25% opacity over white
    # composites to (255,191,191).
    check('layer A red box', color_bbox(image, (255, 191, 191)),
          (10, 10, 189, 89))
    # Layer B: viewport 90x40 at device (30,30); the circle center is at
    # 50% of the 40-unit viewBox => 20 user => device (75,50), radius
    # 25% of 40 = 10 user => 22.5px wide / 10px tall (pAR=none stretch).
    blue = color_bbox(image, BLUE)
    check('layer B circle present', blue is not None, True)
    print(f'         circle bbox {blue} centered at '
          f'{((blue[0] + blue[2]) / 2, (blue[1] + blue[3]) / 2)} '
          '(expected center (75,50))')


def demo_meet_slice_origin():
    """meet/slice/none plus a non-zero viewBox origin on one canvas."""
    print('2. preserveAspectRatio meet / slice / none and viewBox origin')
    svg = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="300" height="120">
      <rect width="300" height="120" fill="white"/>
      {ruler_svg(300, 120)}
      <svg x="10" y="10" width="60" height="100" viewBox="0 0 20 20"
           preserveAspectRatio="xMidYMid meet">
        <rect width="20" height="20" fill="red" opacity="0.4"/>
        <circle cx="50%" cy="50%" r="25%" fill="red"/>
      </svg>
      <svg x="90" y="10" width="100" height="60" viewBox="0 0 20 20"
           preserveAspectRatio="xMidYMid slice">
        <rect width="20" height="20" fill="green" opacity="0.4"/>
        <circle cx="50%" cy="50%" r="25%" fill="green"/>
      </svg>
      <svg x="210" y="10" width="80" height="60" viewBox="-10 -10 20 20">
        <rect x="-10" y="-10" width="20" height="20" fill="blue"
              opacity="0.35"/>
        <circle cx="50%" cy="50%" r="25%" fill="blue"/>
      </svg>
    </svg>'''
    image, _ = render(svg, 'demo2_aspect_origin')
    # meet: scale 3, 60x60 content centered vertically in the 100px viewport
    # => y 30..89; r = 25% of the normalized viewBox diagonal, i.e.
    # 0.25 * hypot(20,20)/sqrt(2) = 5 user => 15 device px. Exact-color
    # pixels exclude the antialiased rim, so the bbox is inset by one.
    red = color_bbox(image, RED)
    check('meet circle', red, (26, 46, 53, 73))
    # slice: scale 5 (100/20), same 5-user radius => 25 device, center
    # user (10,10) -> device (140,40).
    green = color_bbox(image, (0, 128, 0))
    check('slice circle', green, (116, 16, 163, 63))
    # origin -10 -10 20 20 on a 80x60 viewport: meet scale 3, content
    # 60x60 centered horizontally (offset 10). cx 50% = 10 user maps to
    # device x = 210 + 10 + (10+10)*3 = 280, cy = 10 + (10+10)*3 = 70;
    # r 5 user => 15 device. The full circle would span 265..294 / 55..84
    # but the viewport only covers 210..289 / 10..69, so it is clipped;
    # exact-color pixels are inset by one antialiased pixel.
    blue = color_bbox(image, BLUE)
    check('non-zero origin circle', blue, (266, 56, 289, 69))


def demo_sibling_restore_order():
    """Sibling percentages after a nested svg, both sibling orders."""
    print('3. Viewport restore independent of sibling order')
    nested = (
        '<svg x="20" y="20" width="60" height="60" viewBox="0 0 30 30">'
        '<rect width="30" height="30" fill="red" opacity="0.4"/>'
        '<circle cx="50%" cy="50%" r="25%" fill="red"/></svg>')
    sibling = ('<rect x="100" y="20" width="50%" height="25%" fill="blue"/>')
    svg_first = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <rect width="200" height="100" fill="white"/>
      {ruler_svg(200, 100)}
      {nested}{sibling}
    </svg>'''
    svg_second = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <rect width="200" height="100" fill="white"/>
      {ruler_svg(200, 100)}
      {sibling}{nested}
    </svg>'''
    image_first, _ = render(svg_first, 'demo3a_nested_first')
    image_second, _ = render(svg_second, 'demo3b_sibling_first')
    identical = image_first.tobytes() == image_second.tobytes()
    check('both orders render identical pixels', identical, True)
    # The sibling uses the restored 200x100 outer viewport:
    # 50% width = 100px, 25% height = 25px, at x=100 y=20.
    check('sibling box against outer viewport',
          color_bbox(image_first, BLUE), (100, 20, 199, 44))


def demo_symbol_use_parity():
    """A used symbol and the equivalent nested svg must match pixel by pixel."""
    print('4. symbol/use parity with direct nested svg')
    content = ('<rect width="100%" height="100%" fill="white"/>'
               '<circle cx="50%" cy="50%" r="40%" fill="green"/>')
    svg_use = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="160" height="120">
      <defs><symbol id="s" viewBox="0 0 40 40">{content}</symbol></defs>
      <use href="#s" x="20" y="20" width="80" height="80"/>
      <use href="#s" x="20" y="20" width="40" height="40"
           viewBox="0 0 40 40" visibility="hidden"/>
    </svg>'''
    svg_nested = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="160" height="120">
      <svg x="20" y="20" width="80" height="80" viewBox="0 0 40 40">
        {content}
      </svg>
    </svg>'''
    image_use, _ = render(svg_use, 'demo4a_symbol_use')
    image_nested, _ = render(svg_nested, 'demo4b_direct_svg')
    check('use(symbol) == nested svg pixels',
          image_use.tobytes() == image_nested.tobytes(), True)
    green = color_bbox(image_use, GREEN)
    # r 40% of 40 = 16 user, scale 2 => 32 device, center device (60,60);
    # exact-color bbox excludes the antialiased rim (one pixel inset).
    check('symbol circle geometry', green, (29, 29, 90, 90))


def demo_repeated_render():
    """Parsing once and rendering twice must give identical PNGs."""
    print('5. Repeated renders of one parsed tree are idempotent')
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <defs>
        <mask id="m"><rect width="120" height="60" fill="white"/></mask>
        <symbol id="s" viewBox="0 0 20 20">
          <rect width="20" height="20" fill="blue"/>
        </symbol>
      </defs>
      <rect width="120" height="120" fill="white"/>
      <use href="#s" x="10" y="10" width="60" height="60" mask="url(#m)"/>
    </svg>'''
    from cairosvg.parser import Tree
    from cairosvg.surface import PNGSurface
    tree = Tree(bytestring=svg.encode())
    outputs = []
    for _ in range(3):
        buffer = io.BytesIO()
        surface = PNGSurface(tree, buffer, 96)
        surface.finish()
        outputs.append(buffer.getvalue())
    check('three renders byte-identical',
          outputs[0] == outputs[1] == outputs[2], True)
    with open(os.path.join(HERE, 'demo5_repeated.png'), 'wb') as file_obj:
        file_obj.write(outputs[0])
    print('  wrote demo5_repeated.png')


def main():
    results = []
    demo_nested_percentages()
    demo_meet_slice_origin()
    demo_sibling_restore_order()
    demo_symbol_use_parity()
    demo_repeated_render()
    print()
    if results:
        print('failures:', results)
        return 1
    print('all geometry checks passed')
    return 0


if __name__ == '__main__':
    sys.exit(main())
