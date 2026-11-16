#!/usr/bin/env python3
"""Offline legend for ``vector-effect: non-scaling-stroke``.

Renders the *same* 100x100 square outline under four transforms -- translation,
rotation, uniform scaling and non-uniform scaling -- once with a plain stroke
and once with ``non-scaling-stroke``, then measures the actual rendered
thickness of the horizontal and vertical (or, for the rotated case, diagonal)
edges in the produced PNG and assembles an annotated comparison montage.

Run (from the repository root)::

    DYLD_FALLBACK_LIBRARY_PATH=<cairo lib path> \\
        .venv/bin/python -I docs/vector-effect/generate_legend.py

All outputs are written next to this script:

* ``legend.png``                 -- plain vs non-scaling 2x4 montage
* ``legend-annotated.png``       -- the same montage with measured thickness
* ``thickness-measurements.json``-- the numeric pixel measurements
"""

import json
import pathlib
import sys

import cairosvg
from PIL import Image, ImageDraw, ImageFont

HERE = pathlib.Path(__file__).resolve().parent

# (label, transform) applied to a group containing the identical square.
VARIANTS = (
    ('translate', 'translate(110, 100)'),
    ('rotate 30°', 'translate(110, 100) rotate(30)'),
    ('scale 2, 2', 'translate(110, 100) scale(2, 2)'),
    ('scale 2, 3', 'translate(110, 100) scale(2, 3)'),
)
STROKE_WIDTH = 4
TILE_W, TILE_H = 220, 200
INK_THRESHOLD = 250  # near-black: measure the stroke core, ignore anti-alias


def build_svg(transform, non_scaling):
    effect = (
        ' vector-effect="non-scaling-stroke"' if non_scaling else '')
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{TILE_W}"
 height="{TILE_H}" viewBox="0 0 {TILE_W} {TILE_H}">
  <rect width="{TILE_W}" height="{TILE_H}" fill="white"/>
  <g transform="{transform}">
    <rect x="-25" y="-25" width="50" height="50" fill="none"
          stroke="black" stroke-width="{STROKE_WIDTH}"{effect}/>
  </g>
</svg>'''.encode()


def render(transform, non_scaling):
    import io
    return Image.open(io.BytesIO(
        cairosvg.svg2png(build_svg(transform, non_scaling)))).convert('RGB')


def max_run(values):
    best = current = 0
    for value in values:
        current = current + 1 if value else 0
        best = max(best, current)
    return best


def normal_thickness(image, point, direction, reach=10):
    pixels = image.load()
    dx, dy = direction
    px, py = point
    values = [
        sum(pixels[round(px + dx * step),
                   round(py + dy * step)]) < INK_THRESHOLD
        for step in range(-reach, reach + 1)]
    return max_run(values)


def edge_thickness(image, midpoint, tangent, normal):
    """Median normal-direction thickness sampled along a straight edge."""
    tx, ty = tangent
    readings = [
        normal_thickness(
            image,
            (midpoint[0] + tx * offset, midpoint[1] + ty * offset),
            normal)
        for offset in range(-10, 11, 2)]
    return sorted(readings)[len(readings) // 2]


# Where two perpendicular edges are sampled, per variant. Entries are
# (edge-A midpoint/tangent/normal, edge-B midpoint/tangent/normal).
SCANS = {
    'translate': (
        ((85, 100), (0, 1), (1, 0)),       # left vertical edge
        ((110, 75), (1, 0), (0, 1))),      # top horizontal edge
    'rotate 30°': (
        ((122.5, 78.35), (0.8660254, 0.5), (-0.5, 0.8660254)),
        ((88.35, 87.5), (-0.8660254, 0.5), (0.5, 0.8660254))),
    'scale 2, 2': (
        ((60, 100), (0, 1), (1, 0)),
        ((110, 50), (1, 0), (0, 1))),
    'scale 2, 3': (
        ((60, 100), (0, 1), (1, 0)),
        ((110, 25), (1, 0), (0, 1))),
}


def measure(image, label):
    scan_a, scan_b = SCANS[label]
    edge_a = edge_thickness(image, *scan_a)
    edge_b = edge_thickness(image, *scan_b)
    return edge_a, edge_b


def main():
    measurements = {}
    tiles = {}
    for label, transform in VARIANTS:
        for non_scaling in (False, True):
            image = render(transform, non_scaling)
            edge_a, edge_b = measure(image, label)
            kind = 'non-scaling' if non_scaling else 'plain'
            tiles[(kind, label)] = image
            measurements[f'{kind} / {label}'] = {
                'edge_a_px': edge_a,
                'edge_b_px': edge_b,
                'uniform': edge_a == edge_b,
            }

    header = 46
    row_label_w = 130
    montage = Image.new(
        'RGB',
        (row_label_w + TILE_W * len(VARIANTS),
         header + TILE_H * 2),
        (255, 255, 255))
    draw = ImageDraw.Draw(montage)
    try:
        font = ImageFont.truetype(
            '/System/Library/Fonts/Supplemental/Arial.ttf', 16)
        small = ImageFont.truetype(
            '/System/Library/Fonts/Supplemental/Arial.ttf', 13)
    except OSError:  # pragma: no cover - fallback without the system font
        font = small = ImageFont.load_default()

    for column, (label, _) in enumerate(VARIANTS):
        x = row_label_w + column * TILE_W
        draw.text((x + 8, 14), label, fill='black', font=font)

    for row, kind in enumerate(('plain', 'non-scaling')):
        y = header + row * TILE_H
        draw.text((8, y + TILE_H // 2 - 10), kind, fill='black', font=font)
        for column, (label, _) in enumerate(VARIANTS):
            x = row_label_w + column * TILE_W
            tile = tiles[(kind, label)]
            montage.paste(tile, (x, y))
            m = measurements[f'{kind} / {label}']
            caption = (
                f"A {m['edge_a_px']}px / B {m['edge_b_px']}px "
                f"{'uniform' if m['uniform'] else 'ANISOTROPIC'}")
            draw.text((x + 6, y + TILE_H - 22), caption,
                      fill=(180, 0, 0), font=small)

    montage.save(HERE / 'legend-annotated.png')

    # Also save the clean montage without text overlays.
    clean = Image.new(
        'RGB',
        (row_label_w + TILE_W * len(VARIANTS), header + TILE_H * 2),
        (255, 255, 255))
    clean_draw = ImageDraw.Draw(clean)
    for column, (label, _) in enumerate(VARIANTS):
        clean_draw.text(
            (row_label_w + column * TILE_W + 8, 14), label,
            fill='black', font=font)
    for row, kind in enumerate(('plain', 'non-scaling')):
        y = header + row * TILE_H
        clean_draw.text((8, y + TILE_H // 2 - 10), kind,
                        fill='black', font=font)
        for column, (label, _) in enumerate(VARIANTS):
            clean.paste(
                tiles[(kind, label)],
                (row_label_w + column * TILE_W, y))
    clean.save(HERE / 'legend.png')

    (HERE / 'thickness-measurements.json').write_text(
        json.dumps(measurements, indent=2, ensure_ascii=False) + '\n')

    for key, value in measurements.items():
        print(f"{key}: {value['edge_a_px']}px / {value['edge_b_px']}px "
              f"({'uniform' if value['uniform'] else 'anisotropic'})")
    print(f"\nWrote {HERE / 'legend.png'}")
    print(f"Wrote {HERE / 'legend-annotated.png'}")
    print(f"Wrote {HERE / 'thickness-measurements.json'}")


if __name__ == '__main__':
    sys.exit(main())
