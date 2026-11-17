"""Generate a local PNG demo comparing explicit and smooth path commands.

Run with the project virtual environment, e.g.::

    DYLD_FALLBACK_LIBRARY_PATH=/opt/homebrew/lib \\
        .venv/bin/python -I demo/generate_smooth_demo.py

For every panel the same trajectory is drawn twice:

- red solid: the path expanded with explicit control points (C/Q);
- blue dashed: the equivalent smooth-written path (S/T).

They overlap exactly when CairoSVG saves and resets the reflected control
points correctly. Reference control points are shown as small dots. Panels
cover eligible reflection, interruptions (line/arc/move/close), multiple
subpaths, independent path elements, smooth commands starting a path, and
malformed commands following the existing PointError rule.
"""

import os

import cairosvg

WIDTH, HEIGHT = 900, 970

# Each panel: title, explicit path(s), smooth path(s), reference control dots.
# Coordinates are relative to a 280x170 cell; the renderer offsets each cell.
CELL_W, CELL_H = 300, 178
PANELS = [
    ('1. C then S: eligible reflection',
     ['M20 110 C50 40 75 40 105 110 C135 180 160 180 190 110'],
     ['M20 110 C50 40 75 40 105 110 S160 180 190 110'],
     [(50, 40), (75, 40), (135, 180), (160, 180)]),

    ('2. relative c then s',
     ['M20 110 c30 -70 55 -70 85 0 c30 70 55 70 85 0'],
     ['M20 110 c30 -70 55 -70 85 0 s55 70 85 0'],
     [(50, 40), (75, 40), (135, 180), (160, 180)]),

    ('3. S after a line: state reset',
     ['M20 110 L105 110 C105 110 160 180 190 110'],
     ['M20 110 L105 110 S160 180 190 110'],
     [(105, 110), (160, 180)]),

    ('4. S after an arc: state reset',
     ['M20 120 A30 30 0 0 1 80 120 L105 120 C105 120 160 180 190 120'],
     ['M20 120 A30 30 0 0 1 80 120 L105 120 S160 180 190 120'],
     [(105, 120), (160, 180)]),

    ('5. move opens a new subpath: reset',
     ['M20 90 C50 30 75 30 105 90 M110 150 C110 150 160 180 190 150'],
     ['M20 90 C50 30 75 30 105 90 M110 150 S160 180 190 150'],
     [(110, 150), (160, 180)]),

    ('6. close then restart: reset',
     ['M20 90 C50 30 75 30 105 90 Z M110 150 C110 150 160 180 190 150'],
     ['M20 90 C50 30 75 30 105 90 Z M110 150 S160 180 190 150'],
     [(110, 150), (160, 180)]),

    ('7. Q then T: eligible reflection',
     ['M20 110 Q62 40 105 110 Q148 180 190 110'],
     ['M20 110 Q62 40 105 110 T190 110'],
     [(62, 40), (148, 180)]),

    ('8. T after a line: state reset',
     ['M20 110 L105 110 Q105 110 190 110'],
     ['M20 110 L105 110 T190 110'],
     [(105, 110)]),

    ('9. S as the first curve command',
     ['M20 110 C20 110 160 180 190 110'],
     ['M20 110 S160 180 190 110'],
     [(20, 110), (160, 180)]),

    ('10. T as the first curve command',
     ['M20 110 Q20 110 190 110'],
     ['M20 110 T190 110'],
     [(20, 110)]),

    ('11. chained S with repeated groups',
     ['M10 120 C40 50 65 50 95 120 C125 190 150 190 180 120 '
      'C210 50 235 50 265 120'],
     ['M10 120 C40 50 65 50 95 120 S150 190 180 120 235 50 265 120'],
     [(125, 190), (150, 190), (210, 50), (235, 50)]),

    ('12. two independent path elements',
     ['M20 90 C50 30 75 30 105 90',
      'M110 150 C110 150 160 180 190 150'],
     ['M20 90 C50 30 75 30 105 90',
      'M110 150 S160 180 190 150'],
     [(110, 150), (160, 180)]),
]

# Malformed data follows the existing PointError rule: only the valid prefix
# is rendered, no stale smooth curve appears.
ERROR_PATHS = [
    ('C prefix only (S has missing coordinates)',
     'M20 100 C50 40 75 40 105 100 S150'),
    ('Q prefix only (T has no coordinates)',
     'M20 100 Q62 40 105 100 T'),
]


def _shift(path_d, ox, oy):
    """Translate absolute coordinates in a path string; keep relative ones."""
    import re
    parts = re.findall(r'([A-Za-z])|(-?\d+(?:\.\d+)?)', path_d)
    tokens = [letter or number for letter, number in parts]
    out, index = [], 0
    while index < len(tokens):
        command = tokens[index]
        index += 1
        out.append(command)
        absolute = command.isupper()
        value_index = 0
        while index < len(tokens) and not tokens[index].isalpha():
            if absolute:
                offset = ox if value_index % 2 == 0 else oy
                out.append(str(round(float(tokens[index]) + offset, 2)))
            else:
                out.append(tokens[index])
            value_index += 1
            index += 1
    return ' '.join(out)


def build_svg():
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        'font-family="Helvetica, Arial, sans-serif">',
        '<rect width="100%" height="100%" fill="white"/>',
        '<text x="20" y="32" font-size="21" font-weight="bold">'
        'Smooth path commands: explicit control points vs S/T writing</text>',
        '<text x="20" y="54" font-size="13" fill="#555">'
        'red solid = explicit C/Q reference · blue dashed = S/T equivalent '
        '(overlap means identical) · dots = reference control points</text>',
    ]

    for index, (title, explicit, smooth, points) in enumerate(PANELS):
        col, row = index % 3, index // 3
        ox, oy = col * CELL_W, 64 + row * CELL_H
        parts.append(
            f'<rect x="{ox + 8}" y="{oy + 4}" width="{CELL_W - 16}" '
            f'height="{CELL_H - 10}" fill="none" stroke="#e0e0e0"/>')
        parts.append(
            f'<text x="{ox + 16}" y="{oy + 22}" font-size="12.5" '
            f'fill="#222">{title}</text>')
        for path_d in explicit:
            parts.append(
                f'<path d="{_shift(path_d, ox, oy)}" fill="none" '
                'stroke="#d62728" stroke-width="3" stroke-linecap="round" '
                'opacity="0.45"/>')
        for path_d in smooth:
            parts.append(
                f'<path d="{_shift(path_d, ox, oy)}" fill="none" '
                'stroke="#1f77b4" stroke-width="2.5" '
                'stroke-dasharray="6 4" stroke-linecap="round"/>')
        for cx, cy in points:
            parts.append(
                f'<circle cx="{cx + ox}" cy="{cy + oy}" r="3" '
                'fill="#d62728"/>')

    error_y = 64 + 4 * CELL_H
    parts.append(
        '<text x="20" y="' + str(error_y + 12) + '" font-size="13" '
        'fill="#222">13. malformed smooth commands: PointError rule, '
        'only the valid C/Q prefix is drawn</text>')
    for i, (caption, bad_d) in enumerate(ERROR_PATHS):
        cell_x = i * 440
        parts.append(
            f'<rect x="{cell_x + 8}" y="{error_y + 22}" width="424" '
            'height="92" fill="none" stroke="#e0e0e0"/>')
        parts.append(
            f'<text x="{cell_x + 16}" y="{error_y + 40}" font-size="12" '
            f'fill="#555">{caption}</text>')
        parts.append(
            f'<path d="{_shift(bad_d, cell_x, error_y + 10)}" fill="none" '
            'stroke="#7f7f7f" stroke-width="2.5"/>')

    legend_y = HEIGHT - 24
    parts.append(
        f'<line x1="20" y1="{legend_y}" x2="60" y2="{legend_y}" '
        'stroke="#d62728" stroke-width="3" opacity="0.45"/>')
    parts.append(
        f'<text x="68" y="{legend_y + 5}" font-size="13">'
        'explicit control points</text>')
    parts.append(
        f'<line x1="260" y1="{legend_y}" x2="300" y2="{legend_y}" '
        'stroke="#1f77b4" stroke-width="2.5" stroke-dasharray="6 4"/>')
    parts.append(
        f'<text x="308" y="{legend_y + 5}" font-size="13">'
        'smooth S/T writing</text>')
    parts.append(
        f'<circle cx="500" cy="{legend_y}" r="3" fill="#d62728"/>')
    parts.append(
        f'<text x="512" y="{legend_y + 5}" font-size="13">'
        'reference control point</text>')
    parts.append('</svg>')
    return '\n'.join(parts)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    svg = build_svg()
    svg_path = os.path.join(here, 'smooth_paths_demo.svg')
    png_path = os.path.join(here, 'smooth_paths_demo.png')
    with open(svg_path, 'w', encoding='utf-8') as file_obj:
        file_obj.write(svg)
    cairosvg.svg2png(bytestring=svg.encode(), write_to=png_path,
                     output_width=WIDTH, output_height=HEIGHT)
    print(f'wrote {svg_path}')
    print(f'wrote {png_path}')


if __name__ == '__main__':
    main()
