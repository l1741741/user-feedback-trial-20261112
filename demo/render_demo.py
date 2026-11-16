#!/usr/bin/env python3
"""Render the mask demo SVGs and check pixels of the fresh output.

Run from the repository root with the project venv:

    .venv/bin/python demo/render_demo.py

For every SVG in ``demo/svg/`` this script writes a fresh PNG to
``demo/output/``, checks a few pixels of that exact render, and regenerates
``demo/output/index.html`` pairing each input (source + browser rendering)
with the PNG rendered in this run.

"""

import html
import io
import sys
from pathlib import Path

import cairocffi as cairo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import cairosvg  # noqa: E402

DEMO_DIR = Path(__file__).resolve().parent
SVG_DIR = DEMO_DIR / 'svg'
OUTPUT_DIR = DEMO_DIR / 'output'
SCALE = 2  # render at 2x so the PNGs are easy to inspect

# Pixel checks run against the PNG rendered in this run. Coordinates are
# device pixels (user units * SCALE), expectations are (r, g, b, a).
RED = (200, 30, 30, 255)
GREEN = (30, 140, 60, 255)
GRAY_BG = (230, 230, 230, 255)
HALF_RED = (215, 130, 130, 255)      # red at 50% over the gray background
QUARTER_RED = (223, 180, 180, 255)   # red at 25% over the gray background

CHECKS = {
    'luminance-levels': [
        # black -> hidden (background shows), gray -> 50%, white -> solid,
        # semi-transparent white -> 50%
        ((70, 120), GRAY_BG),
        ((170, 120), HALF_RED),
        ((270, 120), RED),
        ((370, 120), HALF_RED),
    ],
    'mask-type-alpha': [
        # alpha mask: black/gray/white all mask fully, 0.5 alpha halves
        ((70, 120), RED),
        ((170, 120), RED),
        ((270, 120), RED),
        ((370, 120), HALF_RED),
    ],
    'gradient-edge': [],  # checked separately (monotonic fade)
    'reuse': [
        # each object is half-masked relative to its own bounding box
        ((50, 60), GREEN), ((100, 60), GRAY_BG),
        ((250, 70), GREEN), ((390, 70), GRAY_BG),
        ((100, 240), GREEN), ((190, 240), GRAY_BG),
        ((340, 240), GREEN), ((410, 240), GRAY_BG),
    ],
    'transform': [
        # translated group: mask boundary at device x = 60 user units
        ((160, 100), RED), ((60, 100), GRAY_BG),
        # rotated group: points on either side of the rotated boundary
        ((335, 220), RED), ((265, 180), GRAY_BG),
    ],
    'nested-opacity': [
        ((70, 120), HALF_RED),      # nested group opacity 0.5 in mask
        ((170, 120), HALF_RED),     # white mask x element opacity 0.5
        ((270, 120), QUARTER_RED),  # 50% gray mask x element opacity 0.5
        ((370, 120), HALF_RED),     # reference: opacity 0.5, no mask
    ],
}


def pixel(image, x, y):
    data = image.get_data()
    offset = y * image.get_stride() + x * 4
    blue, green, red, alpha = (
        int(value) for value in data[offset:offset + 4])
    return red, green, blue, alpha


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    failures = []
    rows = []
    for svg_path in sorted(SVG_DIR.glob('*.svg')):
        name = svg_path.stem
        png_bytes = cairosvg.svg2png(url=str(svg_path), scale=SCALE)
        png_path = OUTPUT_DIR / f'{name}.png'
        png_path.write_bytes(png_bytes)
        image = cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))

        for (x, y), expected in CHECKS.get(name, []):
            actual = pixel(image, x, y)
            ok = all(abs(a - e) <= 8 for a, e in zip(actual, expected))
            status = 'PASS' if ok else 'FAIL'
            print(f'{status} {name} pixel ({x}, {y}): '
                  f'got {actual}, expected {expected}')
            if not ok:
                failures.append((name, x, y))

        if name == 'gradient-edge':
            # The masked blue rectangle fades from hidden to solid: the
            # blue channel moves from the gray background (230) toward
            # the element's blue (200) as the mask gets whiter.
            blues = [
                pixel(image, x, 120)[2]
                for x in (60, 140, 220, 300, 380)]
            ok = blues == sorted(blues, reverse=True) and (
                blues[0] - blues[-1] > 20)
            print(f'{"PASS" if ok else "FAIL"} gradient-edge '
                  f'fade is monotonic: {blues}')
            if not ok:
                failures.append((name, 'gradient'))

        source = html.escape(svg_path.read_text())
        rows.append(f'''
    <h2>{html.escape(name)}</h2>
    <div class="row">
      <div class="cell">
        <h3>Input (rendered by your browser)</h3>
        <img class="demo" src="../svg/{name}.svg" alt="{name} input">
        <details><summary>SVG source</summary><pre>{source}</pre></details>
      </div>
      <div class="cell">
        <h3>CairoSVG output (rendered in this run)</h3>
        <img class="demo" src="{name}.png" alt="{name} output">
      </div>
    </div>''')

    (OUTPUT_DIR / 'index.html').write_text(f'''<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>CairoSVG mask demo</title>
<style>
  body {{ font-family: sans-serif; margin: 2em; }}
  .row {{ display: flex; gap: 2em; align-items: flex-start; }}
  .cell {{ flex: 1; }}
  img.demo {{ border: 1px solid #999; image-rendering: pixelated; }}
  pre {{ background: #f4f4f4; padding: 1em; overflow: auto; }}
</style>
</head>
<body>
  <h1>CairoSVG mask demo</h1>
  <p>Each row shows an input SVG (rendered by your browser) and the PNG
  rendered by CairoSVG in the run that generated this page.</p>
  {''.join(rows)}
</body>
</html>
''')

    print()
    print(f'Rendered {len(rows)} demo(s) into {OUTPUT_DIR}')
    if failures:
        print(f'FAILURES: {failures}')
        return 1
    print('All pixel checks passed.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
