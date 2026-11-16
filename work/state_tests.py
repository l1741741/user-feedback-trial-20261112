"""Verify state independence, repeated export determinism and textPath."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import hashlib

import cairosvg

FONT = 'Helvetica'

# 1. Repeated export of the same SVG must be byte-identical (determinism).
svg_anchor = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="160">
<rect width="300" height="160" fill="white"/>
<text font-family="{FONT}" font-size="20" text-anchor="end" x="200" y="40"
      fill="#00c">end1</text>
<text font-family="{FONT}" font-size="20" text-anchor="middle" x="200" y="90"
      fill="#c0c">mid2</text>
<text font-family="{FONT}" font-size="20" text-anchor="start" x="200" y="140"
      fill="#c80">sta3</text>
</svg>'''

hashes = []
for _ in range(5):
    png = cairosvg.svg2png(bytestring=svg_anchor.encode(),
                           output_width=300, output_height=160)
    hashes.append(hashlib.sha256(png).hexdigest())
assert len(set(hashes)) == 1, 'repeated exports differ'
print('repeated-export determinism: OK')

# 2. Drawing order must not change positions: same three nodes rendered in a
# different order must give per-node identical ink locations.
def positions(svg):
    import io
    from PIL import Image
    png = cairosvg.svg2png(bytestring=svg.encode(),
                           output_width=300, output_height=160)
    im = Image.open(io.BytesIO(png)).convert('RGBA')
    px = im.load()
    out = {}
    for name, color in (('end', '#00c'), ('mid', '#c0c'), ('sta', '#c80')):
        if len(color) == 4:
            color = '#' + ''.join(c * 2 for c in color[1:])
        rgb = tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
        xs, ys = [], []
        for y in range(im.height):
            for x in range(im.width):
                r, g, b, a = px[x, y]
                if a > 40 and all(abs(v - t) < 30
                                  for v, t in zip((r, g, b), rgb)):
                    xs.append(x)
                    ys.append(y)
        out[name] = (min(xs), min(ys), max(xs), max(ys))
    return out

order_a = positions(svg_anchor)
svg_rev = svg_anchor.replace(
    'end1</text>', 'end1</text>').replace(
    '<text font-family="' + FONT + '" font-size="20" text-anchor="start"',
    'XXX')  # placeholder; build explicit reversed svg below
svg_rev = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="160">
<rect width="300" height="160" fill="white"/>
<text font-family="{FONT}" font-size="20" text-anchor="start" x="200" y="140"
      fill="#c80">sta3</text>
<text font-family="{FONT}" font-size="20" text-anchor="middle" x="200" y="90"
      fill="#c0c">mid2</text>
<text font-family="{FONT}" font-size="20" text-anchor="end" x="200" y="40"
      fill="#00c">end1</text>
</svg>'''
order_b = positions(svg_rev)
for key in order_a:
    assert order_a[key] == order_b[key], (key, order_a[key], order_b[key])
print('draw-order independence: OK', order_a)

# 3. A preceding text with dx/dy must not move a following independent text.
svg_leak = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120">
<rect width="300" height="120" fill="white"/>
<text font-family="{FONT}" font-size="20" x="20" y="40" dx="25" dy="15"
      fill="#070">shifted</text>
<text font-family="{FONT}" font-size="20" x="20" y="100" fill="#707">
plain</text>
</svg>'''
svg_clean = svg_leak.replace(
    '<text font-family="' + FONT + '" font-size="20" x="20" y="40" dx="25" '
    'dy="15"\n      fill="#070">shifted</text>\n', '')
p_leak = positions  # reuse helper with custom colors
import io
from PIL import Image


def purple_box(svg):
    png = cairosvg.svg2png(bytestring=svg.encode(),
                           output_width=300, output_height=120)
    im = Image.open(io.BytesIO(png)).convert('RGBA')
    px = im.load()
    xs, ys = [], []
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            if a > 40 and abs(r - 0x77) < 30 and g < 0x40 and abs(b - 0x77) < 30:
                xs.append(x)
                ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


box_with = purple_box(svg_leak)
box_without = purple_box(svg_clean)
assert box_with == box_without, (box_with, box_without)
print('cross-node dx/dy isolation: OK', box_with)

# 4. textPath still renders without error and produces ink on the path.
svg_path = f'''<svg xmlns="http://www.w3.org/2000/svg" width="300" height="120">
<defs><path id="p" d="M20 80 C 80 20, 160 140, 280 60" fill="none"/></defs>
<text font-family="{FONT}" font-size="18" fill="#a30">
<textPath href="#p">on the path text</textPath></text></svg>'''
png = cairosvg.svg2png(bytestring=svg_path.encode(),
                       output_width=300, output_height=120)
im = Image.open(io.BytesIO(png)).convert('RGBA')
px = im.load()
ink = sum(1 for y in range(im.height) for x in range(im.width)
          if px[x, y][3] > 40 and px[x, y][0] > 120)
assert ink > 100, ink
print('textPath rendering: OK, ink px =', ink)
print('ALL STATE TESTS PASSED')
