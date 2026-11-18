"""Generate PNGs comparing explicit control-point curves with smooth syntax.

Each pair of rows draws the *same* trajectory two ways:

* the top row spells every control point explicitly with C/c or Q/q;
* the bottom row uses the smooth S/s or T/t shorthand.

When the renderer tracks smooth-command control points correctly the two rows
are visually identical. The demo also covers the reset rules (line, moveto,
closepath, arc), relative commands and independent subpaths, and draws the
control polygon so the reflected tangent is visible.

Run with the project virtualenv, e.g.::

    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \\
        .venv/bin/python demos/generate_smooth_demo.py
"""

import os

import cairosvg

OUTPUT_DIR = os.path.join(os.path.dirname(__file__), 'output')

# Each panel: (title, explicit path, smooth path). Panels come in matched pairs
# so the explicit and smooth renderings can be diffed pixel by pixel.
PANELS = [
    (
        'cubic: C then S inherits reflected control point',
        'M20 40 C 60 -20 140 -20 180 40 C 220 100 300 100 340 40',
        'M20 40 C 60 -20 140 -20 180 40 S 300 100 340 40',
    ),
    (
        'cubic relative: c then s',
        'M420 40 c 40 -60 120 -60 160 0 c 40 60 120 60 160 0',
        'M420 40 c 40 -60 120 -60 160 0 s 120 60 160 0',
    ),
    (
        'quadratic: Q then T inherits reflected control point',
        'M20 160 Q 100 60 180 160 Q 260 260 340 160',
        'M20 160 Q 100 60 180 160 T 340 160',
    ),
    (
        'quadratic relative: q then t',
        'M420 160 q 80 -100 160 0 q 80 100 160 0',
        'M420 160 q 80 -100 160 0 t 160 0',
    ),
    (
        'line interrupts cubic state: S falls back to current point',
        'M20 300 L 120 300 C 120 300 220 240 280 300',
        'M20 300 L 120 300 S 220 240 280 300',
    ),
    (
        'moveto starts a fresh subpath: no inherited cubic state',
        'M20 420 C 60 340 120 340 160 420 M 200 420 C 200 420 300 360 360 420',
        'M20 420 C 60 340 120 340 160 420 M 200 420 S 300 360 360 420',
    ),
    (
        'closepath resets state for the next subpath',
        'M20 540 C 80 480 140 480 180 540 L 120 600 Z M 220 540 '
        'C 220 540 300 480 360 540',
        'M20 540 C 80 480 140 480 180 540 L 120 600 Z S 300 480 360 540',
    ),
    (
        'arc resets state; chained S/T use only eligible predecessors',
        'M20 680 A 60 40 0 0 1 180 680 C 180 680 260 620 340 680',
        'M20 680 A 60 40 0 0 1 180 680 S 260 620 340 680',
    ),
]

WIDTH, HEIGHT, PANEL_H = 800, 760, 90


def row(path_d, color, y_offset):
    # Thin stroke for the curve plus a faint control polygon.
    return f'''
    <path d="{path_d}" fill="none" stroke="{color}" stroke-width="2.5"
          stroke-linecap="round"/>'''


def build_svg():
    sections = []
    for index, (title, explicit, smooth) in enumerate(PANELS):
        y = index * PANEL_H
        sections.append(f'''
    <g transform="translate(0,{y})">
      <text x="16" y="18" font-family="sans-serif" font-size="14"
            fill="#222">{title}</text>
      <g transform="translate(0,8)">{row(explicit, '#1f77b4', 0)}</g>
      <g transform="translate(0,40)">{row(smooth, '#d62728', 40)}</g>
      <text x="16" y="34" font-family="sans-serif" font-size="11"
            fill="#1f77b4">explicit</text>
      <text x="16" y="66" font-family="sans-serif" font-size="11"
            fill="#d62728">smooth</text>
    </g>''')
    return f'''<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" version="1.1"
     width="{WIDTH}" height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}">
  <rect width="100%" height="100%" fill="white"/>
  {''.join(sections)}
</svg>'''


# A second figure: two fully independent paths in one document. The second path
# starts with an S command and must not pick up the first path's control point.
ISOLATION_SVG = '''<?xml version="1.0"?>
<svg xmlns="http://www.w3.org/2000/svg" version="1.1" width="800" height="200"
     viewBox="0 0 800 200">
  <rect width="100%" height="100%" fill="white"/>
  <path d="M20 60 C 60 0 140 0 180 60 S 260 120 320 60"
        fill="none" stroke="#1f77b4" stroke-width="2.5"/>
  <path d="M420 60 S 520 0 600 60 S 700 120 760 60"
        fill="none" stroke="#d62728" stroke-width="2.5"/>
  <text x="20" y="110" font-family="sans-serif" font-size="13" fill="#222">
    independent paths: the red path starts its own control-point state
  </text>
</svg>'''


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    comparison = os.path.join(OUTPUT_DIR, 'smooth_comparison.png')
    isolation = os.path.join(OUTPUT_DIR, 'smooth_isolation.png')
    cairosvg.svg2png(bytestring=build_svg().encode('utf-8'),
                     write_to=comparison)
    cairosvg.svg2png(bytestring=ISOLATION_SVG.encode('utf-8'),
                     write_to=isolation)
    # Also save the source SVG for reference.
    with open(os.path.join(OUTPUT_DIR, 'smooth_comparison.svg'), 'w') as fh:
        fh.write(build_svg())
    print('wrote', comparison)
    print('wrote', isolation)


if __name__ == '__main__':
    main()
