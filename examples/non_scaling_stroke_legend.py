"""Offline comparison of “non-scaling-stroke” variants.

Run this script to generate the legend and the measurement report::

    python -m examples.non_scaling_stroke_legend
    python -m examples.non_scaling_stroke_legend --out /tmp/example --pdf

The output directory receives:

- ``legend.svg`` / ``legend.png``: the same outline under translation,
  rotation, uniform scaling and non-uniform scaling, with and without
  ``vector-effect="non-scaling-stroke"``;
- ``outline-<variant>-<stroke>.png``: one isolated outline per variant and
  per stroke mode — the exact images the report measures;
- ``joins.svg`` / ``joins.png``: caps, miter join and dashes;
- ``report.txt``: stroke thickness measured on the generated PNG pixels,
  on horizontal and vertical edges, plus the bounding-box centers that
  prove the geometry still follows the object transform.

All numbers in the report come from the PNG files produced by the same run.
CairoSVG also has vector backends (PDF, PS, EPS, SVG); ``--pdf`` rasterizes
the PDF output with ``pdftoppm`` when that program is installed, so the
report states exactly what was verified for vector output on this machine.

"""

import argparse
import io
import shutil
import subprocess
import sys
from math import cos, radians
from pathlib import Path

from PIL import Image

from cairosvg import svg2png

ZOOM = 3
"""Scale between the SVG viewBox and the PNG pixels of the sheet."""

STROKE = 4
"""Stroke width of the reference outline, in user units."""

CELL_WIDTH = 190
CELL_HEIGHT = 140
"""Size of one legend cell, in user units."""

OUTLINE_WIDTH = 46
OUTLINE_HEIGHT = 30

ROTATION = 30
"""Rotation angle of the “rotate” variant, in degrees."""

VARIANTS = (
    ('translate', 'translate(0 0)', 1, 1),
    ('rotate', f'rotate({ROTATION})', None, None),
    ('uniform-scale', 'scale(2.5)', 2.5, 2.5),
    ('scale-x', 'scale(3.2 1.2)', 3.2, 1.2),
    ('scale-y', 'scale(1.2 3.2)', 1.2, 3.2),
)

COLORS = {'plain': '#d62728', 'non-scaling': '#1f77b4'}
STROKE_MODES = (('plain', 'none'), ('non-scaling', 'non-scaling-stroke'))


def outline_path():
    """Return a rectangle centered on the origin."""
    half_width = OUTLINE_WIDTH / 2
    half_height = OUTLINE_HEIGHT / 2
    return (
        f'M {-half_width} {-half_height} H {half_width} V {half_height} '
        f'H {-half_width} Z')


def decorated_path():
    """Return the rectangle plus a vertical stem and a horizontal arm.

    The stem and the arm add real line joins in the middle of the edges,
    which is what the legend shows.

    """
    half_width = OUTLINE_WIDTH / 2
    half_height = OUTLINE_HEIGHT / 2
    return (
        f'{outline_path()} '
        f'M 0 {-half_height + 5} V {half_height - 5} '
        f'M {-half_width + 5} 0 H {half_width - 5}')


def document(body, width=CELL_WIDTH * 2, height=CELL_HEIGHT):
    """Wrap ``body`` in an SVG whose viewport is ``ZOOM`` times the viewBox."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width * ZOOM}" height="{height * ZOOM}" '
        f'viewBox="0 0 {width} {height}">{body}</svg>').encode()


def legend_svg():
    """Build the comparison sheet, one cell per variant and stroke mode."""
    cells = []
    for row, (name, transform, _, _) in enumerate(VARIANTS):
        for column, (label, vector_effect) in enumerate(STROKE_MODES):
            cells.append(
                f'<g transform="translate({column * CELL_WIDTH + 6} '
                f'{row * CELL_HEIGHT + 6})">'
                f'<rect x="0" y="0" width="{CELL_WIDTH - 12}" '
                f'height="{CELL_HEIGHT - 12}" fill="#ffffff" '
                f'stroke="#dddddd" stroke-width="0.6"/>'
                f'<text x="6" y="16" font-size="9" fill="#444">'
                f'{name} · {label}</text>'
                f'<g transform="translate({(CELL_WIDTH - 12) / 2} '
                f'{CELL_HEIGHT / 2 + 6}) {transform}">'
                f'<path d="{decorated_path()}" fill="none" '
                f'stroke="{COLORS[label]}" stroke-width="{STROKE}" '
                f'vector-effect="{vector_effect}"/></g></g>')
    cells.append(
        f'<text x="6" y="{len(VARIANTS) * CELL_HEIGHT + 24}" '
        f'font-size="10" fill="#333">'
        f'viewport zoom {ZOOM}× · stroke-width {STROKE} · '
        f'red = plain stroke, blue = non-scaling-stroke</text>')
    return document(
        ''.join(cells), height=len(VARIANTS) * CELL_HEIGHT + 40)


def isolated_svg(transform, vector_effect):
    """Build one isolated outline using black paint, for measuring pixels."""
    return document(
        f'<g transform="translate({CELL_WIDTH / 2} {CELL_HEIGHT / 2}) '
        f'{transform}">'
        f'<path d="{outline_path()}" fill="none" stroke="#000000" '
        f'stroke-width="{STROKE}" vector-effect="{vector_effect}"/></g>')


def joins_svg():
    """Caps, miter joins and dashes, plain above, non-scaling below.

    Round caps would visually close the gaps of a short dash pattern, so the
    dash rows use butt caps and the cap/join rows are drawn separately.

    """
    parts = []
    for index, (label, vector_effect) in enumerate(STROKE_MODES):
        y = 30 + index * 75
        parts.append(
            f'<g transform="translate(30 {y}) scale(4 1)" fill="none" '
            f'stroke="{COLORS[label]}" stroke-width="{STROKE}" '
            f'stroke-linecap="round" stroke-linejoin="miter">'
            f'<path d="M 0 0 H 30 V 12" '
            f'vector-effect="{vector_effect}"/></g>')
        parts.append(
            f'<g transform="translate(30 {y + 30}) scale(4 1)" fill="none" '
            f'stroke="{COLORS[label]}" stroke-width="{STROKE}" '
            f'stroke-linecap="butt" stroke-dasharray="8 4">'
            f'<path d="M 0 0 H 60" '
            f'vector-effect="{vector_effect}"/></g>')
    parts.append(
        '<text x="4" y="14" font-size="9" fill="#333">'
        'round caps + miter join</text>')
    parts.append(
        '<text x="4" y="44" font-size="9" fill="#333">dashes 8/4</text>')
    parts.append(
        '<text x="4" y="89" font-size="9" fill="#333">'
        'round caps + miter join</text>')
    parts.append(
        '<text x="4" y="119" font-size="9" fill="#333">dashes 8/4</text>')
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="140">'
        f'{"".join(parts)}</svg>').encode()


def render(data, **kwargs):
    """Rasterize ``data`` onto white and return an RGB image."""
    image = Image.open(io.BytesIO(svg2png(bytestring=data, **kwargs)))
    background = Image.new('RGBA', image.size, (255, 255, 255, 255))
    background.alpha_composite(image.convert('RGBA'))
    return background.convert('RGB')


def coverage(pixel):
    """Return the paint coverage of a white-composited RGB pixel."""
    return 1 - sum(pixel) / 765


def runs(values, threshold=0.02):
    """Return ``[(start, end, coverage, center)]`` for painted samples."""
    found, start, total = [], None, 0.0
    for index, value in enumerate(list(values) + [0.0]):
        if value > threshold:
            if start is None:
                start = index
            total += value
        elif start is not None:
            found.append((
                start, index - 1, round(total, 2),
                round(start + total / 2, 1)))
            start, total = None, 0.0
    return found


def scan_row(image, y):
    """Coverage runs along row ``y``."""
    return runs(
        [coverage(image.getpixel((x, y))) for x in range(image.width)])


def scan_column(image, x):
    """Coverage runs along column ``x``."""
    return runs(
        [coverage(image.getpixel((x, y))) for y in range(image.height)])


def ink_bbox(image):
    """Bounding box of painted pixels, or ``None``."""
    pixels = [
        (x, y) for y in range(image.height) for x in range(image.width)
        if coverage(image.getpixel((x, y))) > 0.08]
    if not pixels:
        return None
    xs = [x for x, _ in pixels]
    ys = [y for _, y in pixels]
    return min(xs), min(ys), max(xs), max(ys)


def measure(image, scans=2):
    """Measure the outline's edges with two scans through its middle.

    ``scans`` is the number of strokes a half-scan is expected to cross.

    """
    box = ink_bbox(image)
    if not box:
        return None
    min_x, min_y, max_x, max_y = box
    middle_x = (min_x + max_x) // 2
    middle_y = (min_y + max_y) // 2
    horizontal = scan_column(image, middle_x)[:scans]
    vertical = scan_row(image, middle_y)[:scans]
    return {
        'bbox': box,
        'center': (
            round((min_x + max_x) / 2, 1), round((min_y + max_y) / 2, 1)),
        'horizontal-edges': horizontal,
        'vertical-edges': vertical,
    }


def expected(scale_x, scale_y):
    """Return expected thicknesses ``(plain, non-scaling)`` per direction.

    Host space is one CSS pixel per unit: a non-scaling stroke keeps the
    ``stroke-width`` value, a plain stroke is multiplied by the viewBox
    zoom and by the object scale. A rotation makes both strokes appear
    ``1 / cos(angle)`` thicker along an axis-aligned scan.

    """
    if scale_x is None:
        return (
            (STROKE * ZOOM / cos(radians(ROTATION)),) * 2,
            (STROKE / cos(radians(ROTATION)),) * 2)
    plain_horizontal = STROKE * ZOOM * scale_y
    plain_vertical = STROKE * ZOOM * scale_x
    return (
        (round(plain_horizontal, 2), round(plain_vertical, 2)),
        (STROKE, STROKE))


def main(arguments=None):
    """Generate the legend, run the measurements and write the report."""
    parser = argparse.ArgumentParser(
        description='Generate the non-scaling-stroke legend and report.')
    parser.add_argument(
        '--out', default=str(Path(__file__).parent / 'output'),
        help='output directory (default: examples/output)')
    parser.add_argument(
        '--pdf', action='store_true',
        help='also rasterize the PDF output when pdftoppm is available')
    options = parser.parse_args(arguments)
    out = Path(options.out)
    out.mkdir(parents=True, exist_ok=True)

    legend = legend_svg()
    (out / 'legend.svg').write_bytes(legend)
    render(legend).save(out / 'legend.png')

    report = [
        'non-scaling-stroke: measurements on generated PNG pixels', '']
    report.append(
        f'Outline: {OUTLINE_WIDTH} x {OUTLINE_HEIGHT} user units, '
        f'stroke-width {STROKE}, drawn in a viewport with a {ZOOM}x viewBox '
        f'zoom.')
    report.append(
        'Thickness is the summed pixel coverage of a scan across the '
        'strokes, in device pixels. A non-scaling stroke keeps the '
        f'stroke-width value in viewport pixels: {STROKE} px, whatever the '
        'object transform. A plain stroke is multiplied by the viewBox zoom '
        f'({ZOOM}x) and by the object scale.')
    report.append('')

    failures = []
    for name, transform, scale_x, scale_y in VARIANTS:
        report.append(f'variant: {name}  transform="{transform}"')
        results = {}
        for label, vector_effect in STROKE_MODES:
            image = render(isolated_svg(transform, vector_effect))
            image.save(out / f'outline-{name}-{label}.png')
            results[label] = measure(image)

        plain = results['plain']
        non_scaling = results['non-scaling']
        (plain_h, plain_v), (non_scaling_h, non_scaling_v) = expected(
            scale_x, scale_y)

        report.append(
            f'  plain        bbox={plain["bbox"]} '
            f'horizontal-edges={plain["horizontal-edges"]} '
            f'vertical-edges={plain["vertical-edges"]}')
        report.append(
            f'  non-scaling  bbox={non_scaling["bbox"]} '
            f'horizontal-edges={non_scaling["horizontal-edges"]} '
            f'vertical-edges={non_scaling["vertical-edges"]}')

        # Only the stroke scale may change: the bounding-box centers must be
        # the same. (The boxes differ by half a stroke width on each side.)
        geometry_ok = all(
            abs(a - b) <= 1.5
            for a, b in zip(plain['center'], non_scaling['center']))
        plain_meas = [
            run[2] for run in plain['horizontal-edges']] + [
            run[2] for run in plain['vertical-edges']]
        plain_expected = [plain_h, plain_h, plain_v, plain_v]
        non_scaling_meas = [
            run[2] for run in non_scaling['horizontal-edges']] + [
            run[2] for run in non_scaling['vertical-edges']]
        non_scaling_expected = [
            non_scaling_h, non_scaling_h, non_scaling_v, non_scaling_v]

        report.append(
            f'  edge centers: plain={plain["center"]} '
            f'non-scaling={non_scaling["center"]} -> '
            f'{"same" if geometry_ok else "DIFFERENT"}')
        report.append(
            f'  thickness: plain={plain_meas} (expected {plain_expected})')
        report.append(
            f'             non-scaling={non_scaling_meas} '
            f'(expected {non_scaling_expected})')

        if not geometry_ok:
            failures.append(f'{name}: geometry changed with the stroke mode')
        if len(plain_meas) != len(plain_expected) or any(
                abs(a - b) > 1.2
                for a, b in zip(plain_meas, plain_expected)):
            failures.append(f'{name}: plain stroke did not scale as expected')
        if len(non_scaling_meas) != len(non_scaling_expected) or any(
                abs(a - b) > 0.8
                for a, b in zip(non_scaling_meas, non_scaling_expected)):
            failures.append(
                f'{name}: non-scaling stroke thickness is not constant')
        report.append('')

    joins = joins_svg()
    (out / 'joins.svg').write_bytes(joins)
    joins_image = render(joins)
    joins_image.save(out / 'joins.png')
    report.append(
        'joins.png: scale(4 1); rows 1 and 3 are round caps and a miter '
        'join, rows 2 and 4 are butt dashes of 8/4; red is plain, blue is '
        'non-scaling.')
    plain_dashes = scan_row(joins_image, 60)[:4]
    non_scaling_dashes = scan_row(joins_image, 135)[:4]
    plain_caps = scan_row(joins_image, 30)[:1]
    non_scaling_caps = scan_row(joins_image, 105)[:1]
    report.append(f'  plain dashes:          {plain_dashes}')
    report.append(f'  non-scaling dashes:    {non_scaling_dashes}')
    report.append(f'  plain capped line:     {plain_caps}')
    report.append(f'  non-scaling capped line: {non_scaling_caps}')
    # Dashes are measured in host space too: plain dashes are 4x longer.
    plain_dash = plain_dashes[0][1] - plain_dashes[0][0] + 1
    non_scaling_dash = non_scaling_dashes[0][1] - non_scaling_dashes[0][0] + 1
    report.append(
        f'  first dash: plain={plain_dash} px, '
        f'non-scaling={non_scaling_dash} px '
        f'(expected {8 * 4} and {8})')
    if abs(plain_dash - 32) > 2 or abs(non_scaling_dash - 8) > 2:
        failures.append('joins: dash lengths were not measured as expected')
    # The round caps of the non-scaling row are STROKE / 2 long, while the
    # plain row has caps stretched 4 times horizontally.
    plain_length = plain_caps[0][1] - plain_caps[0][0] + 1
    non_scaling_length = non_scaling_caps[0][1] - non_scaling_caps[0][0] + 1
    report.append(
        f'  capped line length: plain={plain_length} px, '
        f'non-scaling={non_scaling_length} px (stroke is 4 px; the plain '
        f'caps add 4x2 px, the non-scaling caps add 2x2 px)')
    if abs(plain_length - (30 * 4 + 2 * STROKE * 4 // 2)) > 3:
        failures.append('joins: plain caps were not scaled as expected')
    if abs(non_scaling_length - (30 * 4 + 2 * STROKE // 2)) > 3:
        failures.append('joins: non-scaling caps are not stroke-sized')
    report.append('')

    if options.pdf:
        pdftoppm = shutil.which('pdftoppm')
        if pdftoppm:
            from cairosvg import svg2pdf

            (out / 'legend.pdf').write_bytes(svg2pdf(bytestring=legend))
            subprocess.run(
                [pdftoppm, '-png', '-r', '96', '-singlefile',
                 str(out / 'legend.pdf'), str(out / 'legend-pdf')],
                check=True)
            rasterized = Image.open(str(out / 'legend-pdf.png')).convert(
                'RGB')
            report.append(
                'PDF: legend.pdf written and rasterized to legend-pdf.png '
                f'({rasterized.size[0]}x{rasterized.size[1]}). The vector '
                'output holds the same outline Cairo computed; compare it '
                'with legend.png.')
        else:
            report.append(
                'PDF: pdftoppm is not installed, so the vector output was '
                'not rasterized here and is not verified by this run.')
        report.append('')

    report.append('checks:')
    if failures:
        report.extend(f'  FAILED {failure}' for failure in failures)
    else:
        report.append(
            '  every variant keeps the same edge positions with and without '
            'the vector effect, the plain stroke scales with the transform, '
            'and the non-scaling stroke keeps a constant thickness.')
    report_text = '\n'.join(report)
    (out / 'report.txt').write_text(report_text)
    print(report_text)
    print()
    print(f'Files written to {out}')
    return 1 if failures else 0


if __name__ == '__main__':
    sys.exit(main())
