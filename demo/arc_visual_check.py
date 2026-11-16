#!/usr/bin/env python3
"""Offline visual verification of elliptical arc (A/a) rendering.

For each case this script exports a PNG showing:

* the arc itself (thick black stroke);
* the full ellipse the arc belongs to (dashed gray), computed with the
  SVG 1.1 F.6.5 endpoint-to-center conversion — including the radius
  scale-up applied when the given radii are too small for the chord;
* geometric control markers: center (gray cross), radii lines from the
  center to both endpoints (thin gray), chord (dashed blue), start point
  (green dot), end point (red dot) and a tangent arrow at the arc
  midpoint showing the drawing direction.

Each PNG is then checked automatically:

* endpoints — the stroke reaches both arc endpoints;
* orientation — the arc midpoint is painted while the midpoint of the
  complementary arc (same ellipse, opposite sweep) stays clean, so a
  flipped or mirrored arc fails;
* bounds — the bounding box of all painted pixels stays within the
  expected arc bounding box (± stroke/2 + 2 px antialiasing margin), so
  endpoint spikes and full-ellipse artifacts fail.

Run from the repository root with the project virtualenv:

    export DYLD_FALLBACK_LIBRARY_PATH="/opt/homebrew/lib"  # adapt locally
    .venv/bin/python demo/arc_visual_check.py

PNGs are written to demo/output/. Exit code is 0 when all checks pass.
"""

import io
import math
import sys
from pathlib import Path

import cairocffi as cairo

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cairosvg import svg2png  # noqa: E402
from cairosvg.test_path_arcs import painted_pixels  # noqa: E402

OUTPUT_DIR = Path(__file__).resolve().parent / 'output'
STROKE = 4
MARGIN = STROKE / 2 + 2  # stroke half-width + antialiasing


def arc_parameters(x1, y1, x2, y2, rx, ry, rotation, large, sweep):
    """SVG 1.1 F.6.5: return (cx, cy, rx, ry, phi, theta1, delta)."""
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(rotation)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy
    lam = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
    if lam > 1:
        scale = math.sqrt(lam)
        rx, ry = rx * scale, ry * scale
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    root = math.sqrt(max(0.0, num / den))
    if large == sweep:
        root = -root
    cxp = root * rx * y1p / ry
    cyp = -root * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2

    def angle(ux, uy, vx, vy):
        cosine = max(-1.0, min(1.0, (ux * vx + uy * vy)
                               / (math.hypot(ux, uy) * math.hypot(vx, vy))))
        result = math.acos(cosine)
        return -result if ux * vy - uy * vx < 0 else result

    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = angle((x1p - cxp) / rx, (y1p - cyp) / ry,
                  (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    return cx, cy, rx, ry, phi, theta1, delta


def ellipse_point(params, theta):
    cx, cy, rx, ry, phi, _, _ = params
    return (cx + rx * math.cos(theta) * math.cos(phi)
            - ry * math.sin(theta) * math.sin(phi),
            cy + rx * math.cos(theta) * math.sin(phi)
            + ry * math.sin(theta) * math.cos(phi))


def transform_points(points, transform):
    """Apply an optional (rotate_deg, sx, sy) group transform to points."""
    if transform is None:
        return points
    angle, sx, sy = transform
    cos_a, sin_a = math.cos(math.radians(angle)), math.sin(
        math.radians(angle))
    return [(cos_a * sx * x - sin_a * sy * y,
             sin_a * sx * x + cos_a * sy * y) for x, y in points]


def case_svg(name, d, arcs, transform=None):
    """Build the verification SVG for a path made of one or more arcs."""
    parts = []
    transform_attribute = ''
    if transform is not None:
        angle, sx, sy = transform
        transform_attribute = (
            f' transform="rotate({angle}) scale({sx} {sy})"')
    parts.append(f'<g{transform_attribute}>')
    for (x1, y1, x2, y2, rx, ry, rotation, large, sweep) in arcs:
        params = arc_parameters(x1, y1, x2, y2, rx, ry, rotation,
                                large, sweep)
        cx, cy, crx, cry, phi, theta1, delta = params
        # Full ellipse (dashed gray)
        parts.append(
            f'<ellipse cx="{cx:.3f}" cy="{cy:.3f}" rx="{crx:.3f}" '
            f'ry="{cry:.3f}" transform="rotate({math.degrees(phi):.3f} '
            f'{cx:.3f} {cy:.3f})" fill="none" stroke="#999" '
            f'stroke-width="1" stroke-dasharray="5 4"/>')
        # Radii lines and chord
        parts.append(
            f'<path d="M {cx:.3f} {cy:.3f} L {x1} {y1} M {cx:.3f} '
            f'{cy:.3f} L {x2} {y2}" fill="none" stroke="#bbb" '
            f'stroke-width="1"/>')
        parts.append(
            f'<path d="M {x1} {y1} L {x2} {y2}" fill="none" '
            f'stroke="#36c" stroke-width="1" stroke-dasharray="3 3"/>')
        # Center cross
        parts.append(
            f'<path d="M {cx - 5:.3f} {cy:.3f} H {cx + 5:.3f} '
            f'M {cx:.3f} {cy - 5:.3f} V {cy + 5:.3f}" stroke="#999" '
            f'stroke-width="1.5"/>')
        # Tangent arrow at arc midpoint (drawing direction)
        mid = ellipse_point(params, theta1 + delta / 2)
        ahead = ellipse_point(params, theta1 + delta / 2 + delta * 0.01)
        ux, uy = ahead[0] - mid[0], ahead[1] - mid[1]
        norm = math.hypot(ux, uy)
        ux, uy = ux / norm, uy / norm
        px, py = -uy, ux
        tip = (mid[0] + 14 * ux, mid[1] + 14 * uy)
        left = (mid[0] + 4 * px, mid[1] + 4 * py)
        right = (mid[0] - 4 * px, mid[1] - 4 * py)
        parts.append(
            f'<path d="M {tip[0]:.3f} {tip[1]:.3f} L {left[0]:.3f} '
            f'{left[1]:.3f} L {right[0]:.3f} {right[1]:.3f} z" '
            f'fill="#c60"/>')
        # Endpoint markers
        parts.append(f'<circle cx="{x1}" cy="{y1}" r="4" fill="green"/>')
        parts.append(f'<circle cx="{x2}" cy="{y2}" r="4" fill="red"/>')
    # The arc itself, on top
    parts.append(
        f'<path d="{d}" fill="none" stroke="black" '
        f'stroke-width="{STROKE}"/>')
    parts.append('</g>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="360" '
        f'height="360"><rect width="360" height="360" fill="white"/>'
        f'{"".join(parts)}</svg>')


def check_case(name, d, arcs, transform=None, extra_segments=()):
    """Render one case, write the PNG, run the geometric checks."""
    png = svg2png(case_svg(name, d, arcs, transform))
    (OUTPUT_DIR / f'{name}.png').write_bytes(png)

    # Pixels of the black arc stroke only (ignore gray/blue/green/red
    # control graphics): black means all channels near 0.
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    width, height, stride = (
        surface.get_width(), surface.get_height(), surface.get_stride())
    data = bytes(surface.get_data())
    black = set()
    for y in range(height):
        for x in range(width):
            offset = y * stride + x * 4
            if (data[offset + 3] >= 32 and data[offset] < 100
                    and data[offset + 1] < 100 and data[offset + 2] < 100):
                black.add((x, y))

    failures = []
    all_reference = []
    for segment in extra_segments:
        points = transform_points(segment, transform)
        all_reference += [
            (points[0][0] + (points[1][0] - points[0][0]) * i / 64,
             points[0][1] + (points[1][1] - points[0][1]) * i / 64)
            for i in range(65)]
    for (x1, y1, x2, y2, rx, ry, rotation, large, sweep) in arcs:
        params = arc_parameters(x1, y1, x2, y2, rx, ry, rotation,
                                large, sweep)
        _, _, _, _, _, theta1, delta = params
        reference = transform_points(
            [ellipse_point(params, theta1 + delta * i / 256)
             for i in range(257)], transform)
        all_reference += reference

        def painted_near(point, radius=MARGIN):
            x0, y0 = point
            return any(
                (x, y) in black
                for x in range(round(x0 - radius), round(x0 + radius) + 1)
                for y in range(round(y0 - radius), round(y0 + radius) + 1)
                if math.hypot(x - x0, y - y0) <= radius)

        # Endpoints
        for label, point in zip('SE', transform_points(
                [(x1, y1), (x2, y2)], transform)):
            if not painted_near(point):
                failures.append(f'endpoint {label} not reached at {point}')
        # Orientation: arc midpoint painted, complementary midpoint clean
        mid = transform_points(
            [ellipse_point(params, theta1 + delta / 2)], transform)[0]
        opposite = transform_points(
            [ellipse_point(params, theta1 + delta / 2 + math.pi)],
            transform)[0]
        if not painted_near(mid):
            failures.append(f'arc midpoint missing at {mid}')
        if painted_near(opposite):
            failures.append(
                f'complementary arc painted at {opposite} (flipped arc?)')

    # Bounds: every black pixel must stay near the reference arc
    for x, y in black:
        if not any(
                math.hypot(x - rx_, y - ry_) <= MARGIN + 1
                for rx_, ry_ in all_reference[::4]):
            failures.append(
                f'ink outside expected bounds at ({x}, {y}) '
                '(spike or full-ellipse artifact?)')
            break
    return failures


CASES = [
    # name, path data, arc parameter tuples, optional group transform
    ('01-rotated-sweep', 'M 90 200 A 60 35 30 0 1 250 170',
     [(90, 200, 250, 170, 60, 35, 30, 0, 1)], None),
    ('02-negative-radii', 'M 90 180 A -55 -30 -20 0 1 260 190',
     [(90, 180, 260, 190, -55, -30, -20, 0, 1)], None),
    ('03-large-arc-rotation', 'M 100 180 A 45 70 75 1 0 250 180',
     [(100, 180, 250, 180, 45, 70, 75, 1, 0)], None),
    ('04-insufficient-radii', 'M 80 180 A 20 15 0 0 1 280 180',
     [(80, 180, 280, 180, 20, 15, 0, 0, 1)], None),
    ('05-consecutive-arcs',
     'M 60 180 A 35 25 15 0 1 140 180 A 35 25 15 1 1 220 180 '
     'a 35 25 15 0 0 80 0',
     [(60, 180, 140, 180, 35, 25, 15, 0, 1),
      (140, 180, 220, 180, 35, 25, 15, 1, 1),
      (220, 180, 300, 180, 35, 25, 15, 0, 0)], None),
    ('06-degenerate-segments',
     'M 60 180 a 0 20 0 0 1 60 0 a 30 20 0 0 1 0 0 '
     'a 40 25 10 0 1 90 0 l 0 60',
     [(120, 180, 210, 180, 40, 25, 10, 0, 1)], None,
     [((60, 180), (120, 180)), ((210, 180), (210, 240))]),
    ('07-scaled-rotated-group',
     'M 60 40 A 50 30 25 0 1 180 50 a 40 20 -15 1 0 70 10',
     [(60, 40, 180, 50, 50, 30, 25, 0, 1),
      (180, 50, 250, 60, 40, 20, -15, 1, 0)],
     (12, 1.25, 1.25)),
]


def main():
    OUTPUT_DIR.mkdir(exist_ok=True)
    failed = 0
    for name, d, arcs, transform, *rest in CASES:
        failures = check_case(
            name, d, arcs, transform, *(rest or [()]))
        status = 'PASS' if not failures else 'FAIL'
        print(f'{status} {name} -> demo/output/{name}.png')
        for failure in failures:
            failed += 1
            print(f'     - {failure}')
    if failed:
        print(f'{failed} check(s) failed')
        return 1
    print(f'all {len(CASES)} cases pass, PNGs in {OUTPUT_DIR}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
