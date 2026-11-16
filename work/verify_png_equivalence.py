"""Pixel-level equivalence check for the demo rows.

For every row of work/demo_smooth_png.py, rasterize the explicit-control
path and the smooth path into two identical canvases and assert that their
stroke masks coincide (allowing only antialiasing fringe differences).
Also renders the interrupted/closed variants and writes a side-by-side PNG
visualizing any divergence (expected: none after the fix).
"""
import io
import sys

import cairosvg
from PIL import Image

sys.path.insert(0, '/Users/lichao/.local/share/user-feedback/graphics/20261117/render-20261117-12592-path-smooth/work')
from demo_smooth_png import ROWS  # noqa: E402

CANVAS = (200, 90)


def render_mask(d):
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{CANVAS[0]}" '
        f'height="{CANVAS[1]}" viewBox="0 0 200 90">'
        f'<rect width="100%" height="100%" fill="white"/>'
        f'<path d="{d}" fill="none" stroke="black" stroke-width="4"/>'
        f'</svg>')
    png = cairosvg.svg2png(bytestring=svg.encode())
    img = Image.open(io.BytesIO(png)).convert('L')
    # Stroke mask: pixels noticeably darker than the white background.
    return img.point(lambda v: 255 if v < 200 else 0)


def mask_diff(a, b):
    """Pixels belonging to exactly one of the two stroke masks."""
    pa, pb = a.load(), b.load()
    diff = 0
    total = 0
    for y in range(CANVAS[1]):
        for x in range(CANVAS[0]):
            in_a = pa[x, y] == 255
            in_b = pb[x, y] == 255
            total += in_a or in_b
            diff += in_a != in_b
    return diff, total


failures = []
montage = Image.new('RGB', (CANVAS[0] * 3, CANVAS[1] * len(ROWS)), 'white')
for i, (title, explicit, smooth) in enumerate(ROWS):
    ma = render_mask(explicit)
    mb = render_mask(smooth)
    diff, total = mask_diff(ma, mb)
    ratio = diff / total if total else 0.0
    status = 'OK ' if ratio < 0.02 else 'DIFF'
    print(f'{status} [{i}] {title}: differing pixels {diff}/{total} '
          f'({ratio:.3%})')
    if ratio >= 0.02:
        failures.append(title)
    # Side-by-side: explicit (red channel), smooth (blue channel), overlap
    # shows as black; pure red or pure blue pixels would reveal divergence.
    row = Image.new('RGB', (CANVAS[0] * 3, CANVAS[1]), 'white')
    red = Image.new('RGB', CANVAS, 'white')
    red.paste((220, 40, 40), mask=ma)
    blue = Image.new('RGB', CANVAS, 'white')
    blue.paste((40, 80, 220), mask=mb)
    overlay = Image.blend(red, blue, 0.5)
    row.paste(red, (0, 0))
    row.paste(blue, (CANVAS[0], 0))
    row.paste(overlay, (CANVAS[0] * 2, 0))
    montage.paste(row, (0, i * CANVAS[1]))

out = '/Users/lichao/.local/share/user-feedback/graphics/20261117/render-20261117-12592-path-smooth/work/smooth_equivalence_masks.png'
montage.save(out)
print('wrote', out)
if failures:
    print('EQUIVALENCE FAILURES:', failures)
    sys.exit(1)
print('all explicit/smooth pairs are pixel-equivalent')
