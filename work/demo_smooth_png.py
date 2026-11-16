"""Local demonstration for the smooth-path control-point fix.

Generates a PNG comparing, row by row:

1. a curve written with explicit control points (C/Q) versus the equivalent
   smooth writing (S/T) — the two strokes must overlap exactly;
2. smooth segments interrupted by a straight line, an arc, a move (new
   subpath) and a close — the smooth segment after the interruption uses the
   current point as its first control point instead of a stale one.

Run with the project virtualenv and the native Cairo library available, e.g.

    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \\
        .venv/bin/python -I work/demo_smooth_png.py work/smooth_demo.png
"""
import os
import sys

import cairosvg

WIDTH, HEIGHT = 760, 620

# Each row: (title, explicit d, smooth d). Rows are translated into panels.
ROWS = [
    (
        'cubic: C + explicit C  ==  C + S',
        'M20 70 C 45 10 75 10 100 70 C 125 130 150 130 175 70',
        'M20 70 C 45 10 75 10 100 70 S 150 130 175 70',
    ),
    (
        'relative: c + c  ==  c + s (chained, repeated groups)',
        'm20 70 c 25 -60 55 -60 80 0 c 25 60 50 60 75 0',
        'm20 70 c 25 -60 55 -60 80 0 s 50 60 75 0',
    ),
    (
        'quadratic: Q + Q  ==  Q + T',
        'M20 70 Q 50 10 100 70 Q 150 130 180 70',
        'M20 70 Q 50 10 100 70 T 180 70',
    ),
    (
        'line interrupts reflection: ... L 120 70 S ...',
        'M20 70 C 45 10 75 10 100 70 L 120 70 C 120 70 150 130 175 70',
        'M20 70 C 45 10 75 10 100 70 L 120 70 S 150 130 175 70',
    ),
    (
        'arc interrupts reflection: ... A ... S ...',
        'M20 70 C 45 10 75 10 100 70 A 18 18 0 0 1 130 70 '
        'C 130 70 150 130 175 70',
        'M20 70 C 45 10 75 10 100 70 A 18 18 0 0 1 130 70 '
        'S 150 130 175 70',
    ),
    (
        'move starts a fresh subpath: ... M 120 70 S ...',
        'M20 70 C 45 10 75 10 100 70 M 120 70 C 120 70 150 130 175 70',
        'M20 70 C 45 10 75 10 100 70 M 120 70 S 150 130 175 70',
    ),
    (
        'close resets, relative smooth restarts: ... Z s ...',
        'M20 70 C 45 10 75 10 100 70 Z C 20 70 120 20 175 70',
        'M20 70 C 45 10 75 10 100 70 Z s 100 -50 155 0',
    ),
]

PANEL_H = 78
LEFT = 250


def panel_svg(index, title, explicit, smooth):
    y = 12 + index * PANEL_H
    # Two identical coordinate systems side by side would double the width;
    # instead overlay the explicit (blue, wide translucent) and the smooth
    # (red, thin) paths in one panel: equivalence means one color is hidden.
    return f'''
    <text x="12" y="{y + 44}" font-family="Helvetica" font-size="13"
          fill="#222">{title}</text>
    <g transform="translate({LEFT - 210},{y - 4})">
      <path d="{explicit}" fill="none" stroke="#1d5fd1" stroke-width="9"
            stroke-linecap="round" opacity="0.45"/>
      <path d="{smooth}" fill="none" stroke="#d1271d" stroke-width="2.5"
            stroke-linecap="round"/>
    </g>'''


def build_svg():
    panels = '\n'.join(
        panel_svg(i, title, explicit, smooth)
        for i, (title, explicit, smooth) in enumerate(ROWS))
    # Legend and a per-panel baseline.
    baselines = '\n'.join(
        f'<line x1="{LEFT - 208}" y1="{12 + i * PANEL_H + 66}" '
        f'x2="{WIDTH - 14}" y2="{12 + i * PANEL_H + 66}" '
        f'stroke="#ddd" stroke-width="1"/>'
        for i in range(len(ROWS)))
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}"
     height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">
  <rect width="100%" height="100%" fill="white"/>
  <text x="12" y="22" font-family="Helvetica" font-size="15"
        font-weight="bold" fill="#111">
    Smooth path control points: explicit (blue) vs smooth notation (red)
  </text>
  <text x="12" y="40" font-family="Helvetica" font-size="12" fill="#555">
    Equivalent trajectories overlap exactly, so the thin red line covers the
    wide blue stroke everywhere.
  </text>
  {baselines}
  {panels}
  <g font-family="Helvetica" font-size="12">
    <line x1="{WIDTH - 150}" y1="34" x2="{WIDTH - 130}" y2="34"
          stroke="#1d5fd1" stroke-width="9" opacity="0.45"/>
    <text x="{WIDTH - 124}" y="38" fill="#333">explicit C/Q</text>
    <line x1="{WIDTH - 150}" y1="50" x2="{WIDTH - 130}" y2="50"
          stroke="#d1271d" stroke-width="2.5"/>
    <text x="{WIDTH - 124}" y="54" fill="#333">smooth S/T</text>
  </g>
</svg>'''


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else 'work/smooth_demo.png'
    png = cairosvg.svg2png(bytestring=build_svg().encode(), output_width=WIDTH,
                           output_height=HEIGHT)
    with open(out, 'wb') as fd:
        fd.write(png)
    print(f'wrote {os.path.abspath(out)} ({len(png)} bytes)')


if __name__ == '__main__':
    main()
