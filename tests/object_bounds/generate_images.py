"""Generate local, regenerable regression images for the object-bounds fix.

Run from the repository root with:

    DYLD_FALLBACK_LIBRARY_PATH=<cairo-lib-path> \\
        .venv/bin/python -I tests/object_bounds/generate_images.py

The script writes one ``.svg`` source and one rendered ``.png`` per fixture
into ``tests/object_bounds/images/``. Every image is derived deterministically
from the SVG text embedded here, so deleting the PNGs and re-running this
script regenerates them (Cairo output is deterministic for a given library
version).

Each fixture pairs a curved path with an ``objectBoundingBox`` effect so that
an incorrect geometry box visibly shifts, stretches or (for clips) prevents
the rendering. The paths are chosen so that their analytic extrema lie well
inside their control polygon — exactly the situation the old control-point
based bounding box got wrong.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from cairosvg import svg2png  # noqa: E402

IMAGES = Path(__file__).resolve().parent / 'images'

FIXTURES = {
    'gradient-cubic': '''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
  <defs>
    <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="red"/>
      <stop offset="0.5" stop-color="lime"/>
      <stop offset="1" stop-color="blue"/>
    </linearGradient>
  </defs>
  <rect width="200" height="200" fill="white"/>
  <!-- Control polygon spans y=0..100; the real curve only spans y=25..100,
       so the old code stretched the gradient over 100 units instead of 75. -->
  <path d="M20 100 C20 0 180 0 180 100 Z" fill="url(#g)"/>
</svg>
''',
    'gradient-smooth-cubic': '''<svg xmlns="http://www.w3.org/2000/svg" width="220" height="180">
  <defs>
    <linearGradient id="r" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="yellow"/>
      <stop offset="0.5" stop-color="seagreen"/>
      <stop offset="1" stop-color="purple"/>
    </linearGradient>
  </defs>
  <rect width="220" height="180" fill="white"/>
  <!-- Smooth cubic S: the old code used the endpoint-only box for the S
       segment, so its vertical gradient was stretched over y=40..150 instead
       of the tight y=53.7..150. -->
  <path d="M20 120 C40 40 80 40 100 80 S160 120 180 60 L180 150 L20 150 Z"
        fill="url(#r)"/>
</svg>
''',
    'clip-curves': '''<svg xmlns="http://www.w3.org/2000/svg" width="220" height="180">
  <defs>
    <clipPath id="c" clipPathUnits="objectBoundingBox">
      <rect x="0.1" y="0.1" width="0.8" height="0.8"/>
    </clipPath>
  </defs>
  <rect width="220" height="180" fill="white"/>
  <!-- On the old code this raises CAIRO_STATUS_INVALID_MATRIX, because
       objectBoundingBox clips scaled by the path's zero width/height. -->
  <path d="M20 140 C20 40 200 40 200 140 Z" fill="crimson"
        clip-path="url(#c)"/>
</svg>
''',
    'pattern-curves': '''<svg xmlns="http://www.w3.org/2000/svg" width="220" height="180">
  <defs>
    <!-- No viewBox: with objectBoundingBox pattern units CairoSVG scales the
         tile coordinate system by the geometry box, so an inflated box (the
         old T/t result) enlarges every stripe directly. -->
    <pattern id="p" width="0.12" height="0.12"
             patternContentUnits="objectBoundingBox">
      <rect width="0.12" height="0.12" fill="white"/>
      <rect width="0.06" height="0.12" fill="teal"/>
    </pattern>
  </defs>
  <rect width="220" height="180" fill="white"/>
  <!-- T/t smooth quadratic: the old code dropped the reflected control point,
       inflating the y-box (height 140 instead of the tight 105.6) and so
       enlarging every pattern tile. -->
  <path d="M20 130 Q60 20 110 70 T200 130 L200 160 L20 160 Z"
        fill="url(#p)"/>
</svg>
''',
    'transform-reuse': '''<svg xmlns="http://www.w3.org/2000/svg" width="220" height="180">
  <defs>
    <linearGradient id="g2" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="orange"/>
      <stop offset="1" stop-color="navy"/>
    </linearGradient>
  </defs>
  <rect width="220" height="180" fill="white"/>
  <!-- One gradient definition reused under two transforms; each reference
       resolves against its own transformed geometry box. -->
  <g transform="translate(0,-10) rotate(-10 100 90)">
    <path d="M40 100 C40 30 160 30 160 100 Z" fill="url(#g2)"/>
  </g>
  <g transform="translate(0,40) rotate(8 100 90)">
    <path d="M40 100 C40 30 160 30 160 100 Z" fill="url(#g2)"/>
  </g>
</svg>
''',
}


def main():
    IMAGES.mkdir(parents=True, exist_ok=True)
    for name, svg in FIXTURES.items():
        svg_path = IMAGES / f'{name}.svg'
        png_path = IMAGES / f'{name}.png'
        svg_path.write_text(svg, encoding='utf-8')
        png = svg2png(svg.encode())
        png_path.write_bytes(png)
        print(f'wrote {svg_path.name} and {png_path.name} '
              f'({len(png)} PNG bytes)')


if __name__ == '__main__':
    main()
