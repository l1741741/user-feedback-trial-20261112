"""Reproduce text-chunk / anchor bugs in CairoSVG text layout."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cairocffi as cairo
from PIL import Image

import cairosvg

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'demo')
os.makedirs(OUT, exist_ok=True)

FONT = 'Helvetica'
TOL = 2.5  # pixels: anti-aliasing tolerance


def render(svg, name, width=400, height=200):
    png = cairosvg.svg2png(
        bytestring=svg.encode(), output_width=width, output_height=height)
    path = os.path.join(OUT, name)
    with open(path, 'wb') as fd:
        fd.write(png)
    return Image.open(path).convert('RGBA')


def norm_color(color):
    if len(color) == 4:
        color = '#' + ''.join(c * 2 for c in color[1:])
    return color


def ink_components(img, color):
    """Connected ink components (8-connectivity) of one color."""
    color = norm_color(color)
    target = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    px = img.load()
    cells = set()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if a > 40 and all(
                    abs(v - t) < 45 for v, t in zip((r, g, b), target)):
                cells.add((x, y))
    components = []
    while cells:
        start = cells.pop()
        stack = [start]
        xs, ys = [start[0]], [start[1]]
        while stack:
            cx, cy = stack.pop()
            for nx in (cx - 1, cx, cx + 1):
                for ny in (cy - 1, cy, cy + 1):
                    if (nx, ny) in cells:
                        cells.remove((nx, ny))
                        stack.append((nx, ny))
                        xs.append(nx)
                        ys.append(ny)
        components.append((min(xs), min(ys), max(xs), max(ys)))
    components.sort(key=lambda b: (b[1], b[0]))
    return components


def font_extents(text, size=20, family=FONT):
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10)
    ctx = cairo.Context(surf)
    ctx.select_font_face(family)
    ctx.set_font_size(size)
    return ctx.text_extents(text)


def merge_line(components):
    """Merge glyph components belonging to the same visual line."""
    lines = []
    for box in components:
        x0, y0, x1, y1 = box
        for line in lines:
            if not (y1 < line[1] - 2 or y0 > line[3] + 2):
                line[0] = min(line[0], x0)
                line[1] = min(line[1], y0)
                line[2] = max(line[2], x1)
                line[3] = max(line[3], y1)
                break
        else:
            lines.append([x0, y0, x1, y1])
    lines.sort(key=lambda l: l[1])
    return [tuple(l) for l in lines]


# Case 1: single <text> with two x values, middle anchor.
svg1 = f'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="120">
<rect width="400" height="120" fill="white"/>
<line x1="100" y1="0" x2="100" y2="120" stroke="#888"/>
<line x1="300" y1="0" x2="300" y2="120" stroke="#888"/>
<text font-family="{FONT}" font-size="20" text-anchor="middle"
      x="100 300" y="40 80" fill="#d00">ABCD</text></svg>'''

# Case 2: nested tspan dx must not leak to the next sibling line.
svg2 = f'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="140">
<rect width="400" height="140" fill="white"/>
<line x1="60" y1="0" x2="60" y2="140" stroke="#888"/>
<text font-family="{FONT}" font-size="20" fill="#070">
  <tspan x="60" y="40">A<tspan dx="15">B</tspan>C</tspan>
  <tspan x="60" y="90">DEF</tspan>
</text></svg>'''

# Case 3: independent text nodes drawn in mixed anchor order.
svg3 = f'''<svg xmlns="http://www.w3.org/2000/svg" width="400" height="140">
<rect width="400" height="140" fill="white"/>
<line x1="200" y1="0" x2="200" y2="140" stroke="#888"/>
<text font-family="{FONT}" font-size="20" text-anchor="end" fill="#00c"
      x="200" y="40">end1</text>
<text font-family="{FONT}" font-size="20" text-anchor="middle" fill="#c0c"
      x="200" y="80">mid2</text>
<text font-family="{FONT}" font-size="20" text-anchor="start" fill="#c80"
      x="200" y="120">sta3</text>
</svg>'''

# Case 4: middle chunks started via tspan x overrides.
svg4 = f'''<svg xmlns="http://www.w3.org/2000/svg" width="460" height="120">
<rect width="460" height="120" fill="white"/>
<line x1="40" y1="0" x2="40" y2="120" stroke="#888"/>
<line x1="260" y1="0" x2="260" y2="120" stroke="#888"/>
<text font-family="{FONT}" font-size="20" text-anchor="middle" fill="#b0b">
  <tspan x="40" y="50">left</tspan><tspan x="260" y="90">right</tspan>
</text></svg>'''

failures = []


def check(desc, measured, expected):
    ok = abs(measured - expected) <= TOL
    print(f'  [{"OK" if ok else "FAIL"}] {desc}: measured={measured:.1f} '
          f'expected={expected}')
    if not ok:
        failures.append(desc)


for svg, name, spec, scale in [
    (svg1, 'case1.png', [
        ('#d00', 'middle', [(100, 'AB'), (300, 'CD')])], 1.),
    (svg2, 'case2.png', [
        ('#070', 'start-x', [(60, 'ABC'), (60, 'DEF')])], 1.),
    (svg3, 'case3.png', [
        ('#00c', 'end', [(200, 'end1')]),
        ('#c0c', 'middle', [(200, 'mid2')]),
        ('#c80', 'start', [(200, 'sta3')])], 1.),
    (svg4, 'case4.png', [
        ('#b0b', 'middle', [(40, 'left'), (260, 'right')])], 400 / 460),
]:
    img = render(svg, name)
    print(f'--- {name} ---')
    for color, mode, expects in spec:
        lines = merge_line(ink_components(img, color))
        assert len(lines) == len(expects), (color, lines, expects)
        for line, (anchor_x, label) in zip(lines, expects):
            anchor_px = anchor_x * scale
            x0, _, x1, _ = line
            if mode == 'middle':
                check(f'{name} {label} center', (x0 + x1) / 2, anchor_px)
            elif mode == 'start-x':
                ext = font_extents(label)
                check(f'{name} {label} start', x0, anchor_px + ext[0])
            elif mode == 'end':
                # end anchor: advance width ends at the anchor, so the ink's
                # right edge sits at (anchor - (x_advance - (x_bearing+width)))
                ext = font_extents(label)
                x_bearing, _, width, _, x_advance, _ = ext
                expected_right = anchor_px - (x_advance - x_bearing - width)
                check(f'{name} {label} end', x1, expected_right)
            elif mode == 'start':
                ext = font_extents(label)
                check(f'{name} {label} start', x0, anchor_px + ext[0])

print()
print('FAILURES:', failures if failures else 'none')
sys.exit(1 if failures else 0)
