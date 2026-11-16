"""Verify rotate, letter-spacing, stroke and empty-text semantics."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cairosvg
from PIL import Image

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'demo')
os.makedirs(OUT, exist_ok=True)
FONT = 'Helvetica'


def render(svg, name, w=460, h=240):
    cairosvg.svg2png(bytestring=svg.encode(), write_to=os.path.join(OUT, name),
                     output_width=w, output_height=h)
    return Image.open(os.path.join(OUT, name)).convert('RGBA')


# rotate: first glyph rotated 30deg, later glyphs keep last rotation (0).
# letter-spacing: extra advance between glyphs.
# stroke: outlined text must still appear.
# empty text / tspan must not raise and must not move later nodes.
svg = f'''<svg xmlns="http://www.w3.org/2000/svg" width="460" height="240">
<rect width="460" height="240" fill="white"/>
<line x1="40" y1="0" x2="40" y2="240" stroke="#aaa"/>
<line x1="0" y1="60" x2="460" y2="60" stroke="#eee"/>

<text font-family="{FONT}" font-size="24" x="40" y="60" fill="#039"
      letter-spacing="6" rotate="30 0">AB</text>

<text font-family="{FONT}" font-size="24" x="40" y="130" fill="none"
      stroke="#c33" stroke-width="1.5">STROKED</text>

<text font-family="{FONT}" font-size="24" x="40" y="200" fill="#060">
  <tspan></tspan><tspan x="40" y="200">after-empty</tspan>
</text>
</svg>'''

img = render(svg, 'semantics.png')
px = img.load()


def color_bbox(target, tol=30):
    rgb = tuple(int(target[i:i + 2], 16) for i in (1, 3, 5))
    xs, ys = [], []
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if a > 40 and all(abs(v - t) < tol for v, t in zip((r, g, b), rgb)):
                xs.append(x)
                ys.append(y)
    return (min(xs), min(ys), max(xs), max(ys)) if xs else None


blue = color_bbox('#003399')
red = color_bbox('#cc3333')
green = color_bbox('#006600')
print('rotated/spaced blue AB bbox:', blue)
print('stroked red STROKED bbox:', red)
print('green after-empty bbox:', green)

# Rotated glyph A must extend visibly above the baseline row (y < ~40).
assert blue is not None and blue[1] < 45, blue
# Stroked text must render even with fill=none.
assert red is not None and red[2] - red[0] > 60, red
# Empty tspan must not shift the following tspan from x=40.
assert green is not None and abs(green[0] - 40) <= 3, green

# Isolated letter-spacing case (no rotation) measuring B's left edge.
svg_sp = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="80">
<rect width="300" height="80" fill="white"/>
<text font-family="{FONT}" font-size="24" x="40" y="50" fill="#039"
      letter-spacing="__LS__">AB</text></svg>'''
positions = {}
for ls in (0, 10):
    im = render(svg_sp.replace('__LS__', str(ls)), f'spacing_{ls}.png',
                300, 80)
    p = im.load()
    xs = [x for y in range(im.height) for x in range(im.width)
          if p[x, y][3] > 40 and p[x, y][2] > 120 and p[x, y][0] < 90]
    # find the gap: two glyph clusters
    xs = sorted(set(xs))
    groups = []
    for x in xs:
        if not groups or x > groups[-1][1] + 1:
            groups.append([x, x])
        else:
            groups[-1][1] = x
    positions[ls] = groups
    print(f'letter-spacing={ls}: glyph column groups {groups}')
assert len(positions[10]) == 2 and len(positions[0]) == 2
gap_spaced = positions[10][1][0] - positions[10][0][0]
gap_plain = positions[0][1][0] - positions[0][0][0]
print('B left-edge delta from spacing:', gap_spaced - gap_plain)
assert gap_spaced - gap_plain >= 9, (gap_spaced, gap_plain)
print('semantics OK')
