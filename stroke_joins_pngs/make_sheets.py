"""Generate the comparison sheets used to verify the stroke fixes.

Usage::

    python stroke_joins_pngs/make_sheets.py <output-directory>

The script renders each documented case twice: once as a wide comparison
sheet, and once with an inset zoom on the join or cap under test.
"""

import io
import math
import os
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cairosvg  # noqa: E402

SCALE = 8
WIDTH = HEIGHT = 100


def render(body, width=WIDTH, height=HEIGHT):
    source = f'''<svg xmlns="http://www.w3.org/2000/svg"
        xmlns:xlink="http://www.w3.org/1999/xlink"
        width="{width}" height="{height}" viewBox="0 0 {width} {height}">
      <rect width="{width}" height="{height}" fill="white"/>
      {body}
    </svg>'''
    png = cairosvg.svg2png(
        bytestring=source.encode(), output_width=width * SCALE,
        output_height=height * SCALE)
    return Image.open(io.BytesIO(png)).convert('RGB')


def zoom(image, box, factor=3):
    """Crop ``box`` (in user units) and enlarge it with nearest neighbour."""
    left, top, right, bottom = (round(value * SCALE) for value in box)
    crop = image.crop((left, top, right, bottom))
    return crop.resize(
        (crop.width * factor, crop.height * factor), Image.NEAREST)


def sheet(name, description, body, box, output):
    image = render(body)
    image.save(os.path.join(output, f'{name}.png'))
    zoom(image, box).save(os.path.join(output, f'{name}-zoom.png'))
    print(f'{name}: {description}')
    return image



CASES = [
    (
        'sharp-angle-joins',
        'Same sharp V with miter, round and bevel joins. The miter keeps its '
        'long tip, round and bevel are cut back; the red path asks for miter '
        'inside a round group and must keep the miter tip.',
        '''
        <g fill="none" stroke-width="6" stroke-miterlimit="10">
          <path d="M5 70 L25 20 L45 70" stroke="black"
                stroke-linejoin="miter"/>
          <path d="M55 70 L75 20 L95 70" stroke="rgb(0,0,200)"
                stroke-linejoin="bevel"/>
          <g stroke="rgb(200,0,0)" stroke-linejoin="round">
            <path d="M30 70 L50 20 L70 70" stroke-linejoin="miter"/>
          </g>
        </g>''',
        (0, 10, 100, 75),
    ),
    (
        'round-angle-joins',
        'The same three corners with a round join: the tip is replaced by an '
        'arc of radius width/2 on every corner, whatever the miter limit.',
        '''
        <g fill="none" stroke-width="6" stroke-linejoin="round">
          <path d="M10 70 L30 20 L50 70" stroke="black"/>
          <path d="M40 70 L60 20 L80 70" stroke="rgb(200,0,0)"/>
        </g>''',
        (5, 10, 95, 75),
    ),
    (
        'linejoin-override',
        'Left: inherited round join. Right: the same path explicitly asking '
        'for miter. The explicit value used to be ignored.',
        '''
        <g fill="none" stroke="black" stroke-width="6" stroke-miterlimit="10">
          <g stroke-linejoin="round">
            <path d="M10 70 L30 20 L50 70"/>
          </g>
          <g stroke-linejoin="round">
            <path d="M60 70 L80 20 L100 70" stroke-linejoin="miter"/>
          </g>
        </g>''',
        (5, 10, 95, 75),
    ),
    (
        'linecap-override',
        'Three horizontal strokes of width 10: inherited round (top), '
        'explicit butt (middle) and explicit square (bottom). The explicit '
        'butt used to keep the inherited round cap.',
        '''
        <g fill="none" stroke="black" stroke-width="10">
          <g stroke-linecap="round">
            <path d="M20 20 L80 20"/>
            <path d="M20 50 L80 50" stroke="rgb(200,0,0)"
                  stroke-linecap="butt"/>
            <path d="M20 80 L80 80" stroke="rgb(0,0,200)"
                  stroke-linecap="square"/>
          </g>
        </g>''',
        (10, 10, 90, 90),
    ),
    (
        'zero-length-caps',
        'Single-point subpaths: SVG paints a dot with round caps and a square '
        'with square caps, and nothing with butt caps. Cairo paints nothing '
        'in every case, so the shapes were missing.',
        '''
        <g fill="none" stroke-width="12">
          <path d="M15 20 L15 20" stroke="rgb(200,0,0)"
                stroke-linecap="square"/>
          <path d="M40 20 L40 20" stroke="rgb(200,0,0)"
                stroke-linecap="round"/>
          <path d="M65 20 L65 20" stroke="rgb(200,0,0)"
                stroke-linecap="butt"/>
          <path d="M90 20 L90 20" stroke="rgb(200,0,0)"
                stroke-linecap="square"/>
          <path d="M15 55 c0 0 0 0 0 0" stroke="rgb(0,0,200)"
                stroke-linecap="square"/>
          <path d="M40 55 q0 0 0 0" stroke="rgb(0,0,200)"
                stroke-linecap="square"/>
          <line x1="65" y1="55" x2="65" y2="55" stroke="rgb(0,0,200)"
                stroke-linecap="round"/>
          <polyline points="90,55 90,55" stroke="rgb(0,0,200)"
                    stroke-linecap="square"/>
          <path d="M15 85 Z M70 85 L90 85" stroke="rgb(0,128,0)"
                stroke-linecap="square"/>
        </g>''',
        (5, 5, 95, 95),
    ),
    (
        'miterlimit-invalid-values',
        'Sharp V with invalid or hostile miter limits: 0 (kept as a number), '
        '-1, "abc", "4px", "nan" and "inf". None may abort the rendering, and '
        'an infinite limit must not drop the stroke.',
        '''
        <g fill="none" stroke="black" stroke-width="5" stroke-linejoin="miter">
          <path d="M5 62 L20 20 L35 62" stroke-miterlimit="0"/>
          <path d="M27 62 L42 20 L57 62" stroke-miterlimit="-1"/>
          <path d="M49 62 L64 20 L79 62" stroke-miterlimit="abc"/>
          <path d="M71 62 L86 20 L101 62" stroke-miterlimit="4px"/>
        </g>
        <g fill="none" stroke="rgb(200,0,0)" stroke-width="5"
           stroke-linejoin="miter">
          <path d="M16 95 L31 53 L46 95" stroke-miterlimit="nan"/>
          <path d="M58 95 L73 53 L88 95" stroke-miterlimit="inf"/>
        </g>''',
        (0, 10, 100, 100),
    ),
    (
        'mixed-segments',
        'One path mixing a straight line, a quadratic curve, a cubic curve '
        'and a closing segment. Left: miter joins. Right: bevel joins. Every '
        'corner, including the one where the cubic meets the closing line, '
        'follows the selected join.',
        '''
        <g fill="none" stroke-width="5" stroke-miterlimit="10">
          <g transform="translate(0 0)">
            <path d="M8 78 L28 18 L48 78 Q28 60 8 78 Z" stroke="black"
                  stroke-linejoin="miter"/>
          </g>
          <g transform="translate(52 0)">
            <path d="M8 78 L28 18 L48 78 Q28 60 8 78 Z" stroke="black"
                  stroke-linejoin="bevel"/>
          </g>
        </g>''',
        (0, 10, 100, 85),
    ),
    (
        'short-and-zero-segments',
        'Very short segments between long ones, repeated vertices and a '
        'zero-length subpath inside a real path. The outline must stay the '
        'same and the dot must still be painted below the line.',
        '''
        <g fill="none" stroke="black" stroke-width="6" stroke-linejoin="miter"
           stroke-miterlimit="10" stroke-linecap="square">
          <path d="M10 30 L48 25 L48.3 25.2 L86 20"/>
          <path d="M10 55 L10 55 L50 55 L90 55"/>
          <path d="M10 80 L90 80"/>
          <path d="M50 94 L50 94"/>
        </g>''',
        (5, 15, 95, 100),
    ),
    (
        'transformed-sharp-angle',
        'Sharp miter joins inside translated, rotated and scaled groups: the '
        'tip must follow the transform instead of being clipped.',
        '''
        <g fill="none" stroke="black" stroke-width="4" stroke-linejoin="miter"
           stroke-miterlimit="10">
          <g transform="rotate(20 25 55)">
            <path d="M10 70 L25 20 L40 70"/>
          </g>
          <g transform="translate(50 0) scale(0.9 1)">
            <path d="M10 70 L38 20 L66 70"/>
          </g>
        </g>''',
        (0, 0, 100, 100),
    ),
    (
        'invalid-dash-does-not-poison',
        'A path with a negative dash list, followed by a plain rectangle. An '
        'invalid dash list must be ignored; it used to leave the Cairo '
        'context in an error state that swallowed every following shape.',
        '''
        <path d="M10 20 L90 20" fill="none" stroke="black" stroke-width="4"
              stroke-dasharray="10 -5"/>
        <rect x="40" y="60" width="20" height="20" fill="rgb(200,0,0)"/>
        ''',
        (0, 0, 100, 100),
    ),
    (
        'object-bounding-box',
        'Left: an objectBoundingBox clip on a path element. Right: an '
        'objectBoundingBox clip on a circle, which used to scale by zero and '
        'raise an invalid-matrix error.',
        '''
        <defs>
          <clipPath id="half" clipPathUnits="objectBoundingBox">
            <rect width="0.5" height="1"/>
          </clipPath>
          <clipPath id="half2" clipPathUnits="objectBoundingBox">
            <rect width="0.5" height="1"/>
          </clipPath>
        </defs>
        <path d="M10 10 L45 10 L45 90 L10 90 Z" fill="rgb(0,0,200)"
              clip-path="url(#half)"/>
        <circle cx="72" cy="50" r="25" fill="rgb(200,0,0)"
                clip-path="url(#half2)"/>''',
        (0, 0, 100, 100),
    ),
]


def main(output):
    os.makedirs(output, exist_ok=True)
    failures = []
    for name, description, body, box in CASES:
        try:
            sheet(name, description, body, box, output)
        except Exception as exception:  # noqa: BLE001 - evidence collection
            # A crash is itself a result worth recording, especially when the
            # sheets are rendered with the code before the fix.
            failures.append((name, exception))
            with open(os.path.join(output, f'{name}-FAILED.txt'), 'w') as file:
                file.write(f'{type(exception).__name__}: {exception}\n')
            print(f'{name}: FAILED ({type(exception).__name__}: {exception})')
    if failures:
        print(f'\n{len(failures)} case(s) failed to render:')
        for name, exception in failures:
            print(f'  {name}: {type(exception).__name__}: {exception}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'stroke_joins_pngs/after')

