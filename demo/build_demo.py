#!/usr/bin/env python3
"""Build a set of PNG demos for the transform-origin / transform-box work.

Run locally with the project virtual environment::

    export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib"  # if cairo needs it
    .venv/bin/python -I demo/build_demo.py

The script renders small, hand-checkable scenes into ``demo/output`` and
verifies the painted pixel bounding box of each scene against the coordinates
predicted from the CSS Transforms geometry. A scene whose geometry is wrong
fails loudly instead of silently producing a misleading picture.

Every panel draws:

* a light 10-unit grid so axes and centers are readable,
* the untransformed geometry as a dashed outline (the "before" state),
* the transformed geometry in solid red (the "after" state),
* a blue cross on the rotation center and a marker on a tracked corner.

"""

import io
import os
import sys
from math import cos, radians, sin

import cairocffi as cairo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cairosvg import svg2png  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUT = os.path.join(HERE, 'output')


def grid(size, step=10):
    """Return SVG markup for a faint measurement grid and origin axes."""
    lines = []
    for position in range(0, size + 1, step):
        lines.append(
            f'<line x1="{position}" y1="0" x2="{position}" y2="{size}" '
            f'stroke="#e6e6e6" stroke-width="0.5"/>')
        lines.append(
            f'<line x1="0" y1="{position}" x2="{size}" y2="{position}" '
            f'stroke="#e6e6e6" stroke-width="0.5"/>')
    lines.append(
        '<line x1="0" y1="0" x2="0" y2="%d" stroke="#999" stroke-width="1"/>'
        % size)
    lines.append(
        '<line x1="0" y1="0" x2="%d" y2="0" stroke="#999" stroke-width="1"/>'
        % size)
    return ''.join(lines)


def caption(x, y, width, lines, font_size=8):
    """A wrapped bottom caption guaranteed to stay inside the canvas."""
    line_height = font_size + 3
    starts = [y - line_height * (len(lines) - 1 - i) for i in
              range(len(lines))]
    spans = ''.join(
        f'<tspan x="{x}" y="{sy}">{line}</tspan>'
        for line, sy in zip(lines, starts))
    return (f'<text font-size="{font_size}" fill="#333">{spans}</text>')


def center_mark(x, y, label='', label_dx=7, label_dy=-7):
    """A blue cross marking a rotation center."""
    return f'''
      <line x1="{x - 4}" y1="{y}" x2="{x + 4}" y2="{y}"
            stroke="#1155cc" stroke-width="1.2"/>
      <line x1="{x}" y1="{y - 4}" x2="{x}" y2="{y + 4}"
            stroke="#1155cc" stroke-width="1.2"/>
      <circle cx="{x}" cy="{y}" r="1.6" fill="#1155cc"/>
      <text x="{x + label_dx}" y="{y + label_dy}" font-size="9"
            fill="#1155cc">{label}</text>'''


def corner_dot(x, y):
    """A green dot tracking a specific corner."""
    return f'<circle cx="{x}" cy="{y}" r="2.5" fill="#0a8f3c"/>'


def rotate_point(x, y, angle, cx, cy):
    rad = radians(angle)
    return (
        cx + (x - cx) * cos(rad) - (y - cy) * sin(rad),
        cy + (x - cx) * sin(rad) + (y - cy) * cos(rad))


def painted_bbox(png_bytes, dominant='red'):
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))
    buf = bytes(surface.get_data())
    width, height = surface.get_width(), surface.get_height()
    points = []
    for py in range(height):
        for px in range(width):
            offset = (py * width + px) * 4
            blue, green, red, alpha = buf[offset:offset + 4]
            if alpha == 0:
                continue
            if dominant == 'red' and red > green + 40 and red > blue + 40:
                points.append((px, py))
            elif (dominant == 'green' and
                    green > red + 20 and green > blue + 20):
                points.append((px, py))
    if not points:
        return None
    return (
        min(x for x, _ in points), min(y for _, y in points),
        max(x for x, _ in points), max(y for _, y in points))


def render(name, svg, expected_bbox):
    png = svg2png(svg.encode())
    with open(os.path.join(OUTPUT, name), 'wb') as file_obj:
        file_obj.write(png)
    actual = painted_bbox(png)
    status = 'OK ' if actual == expected_bbox else 'BAD'
    print(f'[{status}] {name:32s} expected={expected_bbox} actual={actual}')
    if actual != expected_bbox:
        raise AssertionError(
            f'{name}: expected painted bbox {expected_bbox}, got {actual}')


# ---------------------------------------------------------------------------
# Scene 1: off-center rect rotated about an explicit length origin
# ---------------------------------------------------------------------------

def scene_explicit_origin():
    size = 160
    cx, cy, angle = 20, 20, 90
    # Tracked corner (60, 20) lands at:
    tracked = rotate_point(60, 20, angle, cx, cy)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}"
        height="{size}" viewBox="0 0 {size} {size}">
      {grid(size)}
      <rect x="20" y="20" width="40" height="20" fill="none"
            stroke="#bbbbbb" stroke-dasharray="3 2"/>
      <g transform="rotate({angle})" transform-origin="{cx} {cy}">
        <rect x="20" y="20" width="40" height="20" fill="#d62728"
              fill-opacity="0.85"/>
        {corner_dot(60, 20)}
      </g>
      {center_mark(cx, cy, 'origin (20,20)', 8, 18)}
      {caption(6, size - 8, size, [
          'rotate(90) about point (20,20):',
          f'corner (60,20) -&gt; ({tracked[0]:.0f},{tracked[1]:.0f})'])}
    </svg>'''
    render('01_explicit_length_origin.png', svg, (0, 20, 19, 59))


# ---------------------------------------------------------------------------
# Scene 2: rotation about the object's own fill-box center
# ---------------------------------------------------------------------------

def scene_fill_box():
    size = 160
    angle = 90
    fill_center = (40, 30)
    tracked = rotate_point(60, 20, angle, *fill_center)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}"
        height="{size}" viewBox="0 0 {size} {size}">
      {grid(size)}
      <rect x="20" y="20" width="40" height="20" fill="none"
            stroke="#bbbbbb" stroke-dasharray="3 2"/>
      <rect x="20" y="20" width="40" height="20" fill="#d62728"
            fill-opacity="0.85" transform-box="fill-box"
            transform-origin="50% 50%" transform="rotate({angle})"/>
      {center_mark(*fill_center, label='fill-box center (40,30)',
                   label_dx=8, label_dy=18)}
      {caption(6, size - 8, size, [
          'transform-box=fill-box, origin=50% 50%:',
          f'corner (60,20) -&gt; ({tracked[0]:.0f},{tracked[1]:.0f})'])}
    </svg>'''
    render('02_fill_box_center.png', svg, (30, 10, 49, 49))


# ---------------------------------------------------------------------------
# Scene 3: nested layers — outer translate, inner rotation about a local axis
# ---------------------------------------------------------------------------

def scene_nested_layers():
    width, height = 200, 150
    tx, ty = 50, 0
    inner_center = (30, 30)
    global_center = (inner_center[0] + tx, inner_center[1] + ty)
    tracked_local = rotate_point(50, 20, 90, *inner_center)
    tracked_global = (tracked_local[0] + tx, tracked_local[1] + ty)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}"
        height="{height}" viewBox="0 0 {width} {height}">
      {grid(max(width, height))}
      <g transform="translate({tx} {ty})">
        <rect x="10" y="20" width="40" height="20" fill="none"
              stroke="#bbbbbb" stroke-dasharray="3 2"/>
        <g transform="rotate(90)" transform-origin="30 30">
          <rect x="10" y="20" width="40" height="20" fill="#d62728"
                fill-opacity="0.85"/>
          {corner_dot(50, 20)}
        </g>
        {center_mark(*inner_center, label='',
                     label_dx=0, label_dy=0)}
      </g>
      {center_mark(*global_center, label='global (80,30)',
                   label_dx=9, label_dy=-8)}
      <text x="14" y="78" font-size="9" fill="#1155cc">local (30,30)</text>
      {caption(6, height - 12, width, [
          'outer translate(50,0), then inner rotate(90):',
          f'corner (50,20) -&gt; ({tracked_global[0]:.0f},'
          f'{tracked_global[1]:.0f})'])}
    </svg>'''
    render('03_nested_layers.png', svg, (70, 10, 89, 49))


# ---------------------------------------------------------------------------
# Scene 4: percentage origin on view-box, plus a degenerate box regression
# ---------------------------------------------------------------------------

def scene_view_box_and_degenerate():
    size = 160
    # 25% 75% of the view box is (40, 120).
    origin = (40, 120)
    tracked = rotate_point(20, 40, 90, *origin)
    svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}"
        height="{size}" viewBox="0 0 {size} {size}">
      {grid(size)}
      <rect x="20" y="20" width="40" height="20" fill="none"
            stroke="#bbbbbb" stroke-dasharray="3 2"/>
      <g transform="rotate(90)" transform-origin="25% 75%">
        <rect x="20" y="20" width="40" height="20" fill="#d62728"
              fill-opacity="0.85"/>
      </g>
      {center_mark(*origin, label='(40,120)',
                   label_dx=9, label_dy=-9)}
      {caption(6, size - 8, size, [
          'origin 25% 75% of view-box:',
          f'corner (20,40) -&gt; ({tracked[0]:.0f},{tracked[1]:.0f})'])}
    </svg>'''
    # Rotated bbox: x in [120, 139], y in [100, 139].
    render('04_percentage_view_box.png', svg, (120, 100, 139, 139))

    # Degenerate geometry with a fill-box origin must fall back to the
    # viewport and still allow the sibling rect to render normally.
    degenerate = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}"
        height="{size}" viewBox="0 0 {size} {size}">
      {grid(size)}
      <rect x="0" y="0" width="0" height="0" fill="#d62728"
            transform-box="fill-box" transform-origin="center"
            transform="rotate(90)"/>
      <rect x="50" y="50" width="20" height="20" fill="#d62728"
            fill-opacity="0.85"/>
      {caption(6, size - 8, size, [
          'zero-size rect: fill-box falls back',
          'to view-box; sibling still renders'])}
    </svg>'''
    render('05_degenerate_box_fallback.png', degenerate, (50, 50, 69, 69))


# ---------------------------------------------------------------------------
# Scene 5: illegal values are isolated; reused nodes keep independent results
# ---------------------------------------------------------------------------

def scene_resilience_and_reuse():
    size = 160
    bad = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}"
        height="{size}" viewBox="0 0 {size} {size}">
      {grid(size)}
      <rect x="10" y="10" width="20" height="20" fill="#d62728"
            fill-opacity="0.85" transform-origin="nonsense garbage"
            transform="rotate(90)"/>
      <rect x="90" y="90" width="20" height="20" fill="#0a8f3c"
            fill-opacity="0.85"/>
      {caption(6, size - 8, size, [
          'illegal origin falls back to center;',
          'the green sibling is unaffected'])}
    </svg>'''
    png = svg2png(bad.encode())
    with open(os.path.join(OUTPUT, '06_illegal_value_isolated.png'), 'wb') as f:
        f.write(png)
    red_bbox = painted_bbox(png, 'red')
    green_bbox = painted_bbox(png, 'green')
    print(f'[OK ] 06_illegal_value_isolated.png   red bbox={red_bbox} '
          f'green sibling={green_bbox}')
    # The red rect still paints (center fallback), and the green sibling
    # is rendered at its own declared position.
    assert red_bbox is not None
    assert green_bbox == (90, 90, 109, 109)

    reuse = '''<svg xmlns="http://www.w3.org/2000/svg" width="200"
        height="125" viewBox="0 0 200 125">
      {grid}
      <defs>
        <rect id="r" width="20" height="20" fill="#d62728"
              fill-opacity="0.85" transform-box="fill-box"
              transform-origin="center" transform="rotate(90)"/>
      </defs>
      <use href="#r" x="10" y="40"/>
      <use href="#r" x="70" y="40"/>
      <use href="#r" x="130" y="40"/>
      <text font-size="8" fill="#333">
        <tspan x="6" y="104">same node used three times;</tspan>
        <tspan x="6" y="115">each lands at its own position</tspan>
      </text>
    </svg>'''.format(grid=grid(200))
    render('07_reused_node_independent.png', reuse, (10, 40, 149, 59))

def main():
    os.makedirs(OUTPUT, exist_ok=True)
    scene_explicit_origin()
    scene_fill_box()
    scene_nested_layers()
    scene_view_box_and_degenerate()
    scene_resilience_and_reuse()
    print(f'\nAll demos written to {OUTPUT}')


if __name__ == '__main__':
    main()
