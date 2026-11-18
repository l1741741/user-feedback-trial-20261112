"""Pixel-level equivalence check between explicit and smooth path data.

For every rule exercised by the demo, render two minimal SVGs: one using only
explicit C/c or Q/q control points, the other using the S/s or T/t shorthand.
The two renderings must be byte-identical PNGs, which proves the smooth
commands reproduce the exact same trajectory (and therefore the same stroke
outline and bounding box).

Exits non-zero if any pair differs.
"""

import io
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.pardir))

import cairosvg  # noqa: E402

# (label, explicit path data, smooth path data)
PAIRS = [
    ('cubic C/S',
     'M20 40 C 60 -20 140 -20 180 40 C 220 100 300 100 340 40',
     'M20 40 C 60 -20 140 -20 180 40 S 300 100 340 40'),
    ('cubic c/s relative',
     'M20 40 c 40 -60 120 -60 160 0 c 40 60 120 60 160 0',
     'M20 40 c 40 -60 120 -60 160 0 s 120 60 160 0'),
    ('quadratic Q/T',
     'M20 60 Q 100 -40 180 60 Q 260 160 340 60',
     'M20 60 Q 100 -40 180 60 T 340 60'),
    ('quadratic q/t relative',
     'M20 60 q 80 -100 160 0 q 80 100 160 0',
     'M20 60 q 80 -100 160 0 t 160 0'),
    ('S after line resets to current point',
     'M20 80 L 120 80 C 120 80 240 20 320 80',
     'M20 80 L 120 80 S 240 20 320 80'),
    ('T after line resets to current point',
     'M20 80 L 120 80 Q 120 80 240 80',
     'M20 80 L 120 80 T 240 80'),
    ('S after moveto uses new subpath start',
     'M20 80 C 60 20 120 20 160 80 M 200 80 C 200 80 280 20 340 80',
     'M20 80 C 60 20 120 20 160 80 M 200 80 S 280 20 340 80'),
    ('S after closepath resets state',
     'M20 80 C 60 20 120 20 160 80 L 120 130 Z S 280 20 340 80',
     'M20 80 C 60 20 120 20 160 80 L 120 130 Z C 20 80 280 20 340 80'),
    ('S after arc resets state',
     'M20 80 A 70 40 0 0 1 180 80 C 180 80 260 20 340 80',
     'M20 80 A 70 40 0 0 1 180 80 S 260 20 340 80'),
    ('chained S reflects each previous control point',
     'M20 60 C 50 0 90 0 120 60 C 150 120 190 120 220 60 '
     'C 250 0 290 0 320 60',
     'M20 60 C 50 0 90 0 120 60 S 190 120 220 60 S 290 0 320 60'),
    ('chained T reflects each previous control point',
     'M20 60 Q 70 0 120 60 Q 170 120 220 60 Q 270 0 320 60',
     'M20 60 Q 70 0 120 60 T 220 60 T 320 60'),
]


def render(path_d):
    svg = f'''<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" width="360" height="160"
     viewBox="0 0 360 160">
  <path d="{path_d}" fill="none" stroke="black" stroke-width="3"/>
</svg>'''
    return cairosvg.svg2png(bytestring=svg.encode('utf-8'),
                            output_width=360, output_height=160)


def main():
    failures = []
    for label, explicit, smooth in PAIRS:
        png_explicit = render(explicit)
        png_smooth = render(smooth)
        if png_explicit != png_smooth:
            failures.append(label)
            print(f'FAIL  {label}: PNGs differ '
                  f'({len(png_explicit)} vs {len(png_smooth)} bytes)')
        else:
            print(f'ok    {label}')
    if failures:
        print(f'\n{len(failures)} non-equivalent pair(s)')
        sys.exit(1)
    print(f'\nall {len(PAIRS)} explicit/smooth pairs render identically')


if __name__ == '__main__':
    main()
