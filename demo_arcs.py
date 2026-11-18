#!/usr/bin/env python3
"""Offline visual demo for CairoSVG elliptical arc (A/a) rendering.

Generates ``arc-demo.png`` next to this script, containing:

1. a rotated ellipse drawn with two ``A`` commands (black), overlaid with
   the analytic geometric outline sampled from the SVG F.6.5 reference
   (thin red polyline) — the two must coincide;
2. the ellipse control points: centre cross and the four axis endpoints
   (blue), plus the analytic bounding box (grey dashed);
3. arc segments with arrow markers (orient="auto") whose tips must follow
   the spec tangents (red tangent lines);
4. degenerate cases: identical endpoints (invisible), zero radius
   (straight line), negative radii (identical to absolute radii).

Run offline with the repository's own source tree:

    .venv/bin/python demo_arcs.py

(requires the native cairo library, see NATIVE-RUNTIME.md)
"""

import math
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from cairosvg import svg2png  # noqa: E402


def spec_arc_points(x1, y1, rx, ry, phi, large, sweep, x2, y2, n=96):
    """Sample an arc with the SVG 1.1 F.6.5 endpoint->center conversion."""
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [(x1, y1), (x2, y2)]
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    x1p, y1p = cos_p * dx + sin_p * dy, -sin_p * dx + cos_p * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        rx, ry = math.sqrt(lam) * rx, math.sqrt(lam) * ry
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    coef = math.sqrt(max(0.0, num / den))
    if large == sweep:
        coef = -coef
    cxp, cyp = coef * rx * y1p / ry, -coef * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2
    theta1 = math.atan2((y1p - cyp) / ry, (x1p - cxp) / rx)
    theta2 = math.atan2((-y1p - cyp) / ry, (-x1p - cxp) / rx)
    dtheta = theta2 - theta1
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi
    points = []
    for i in range(n + 1):
        theta = theta1 + dtheta * i / n
        xe, ye = rx * math.cos(theta), ry * math.sin(theta)
        points.append((cx + cos_p * xe - sin_p * ye,
                       cy + sin_p * xe + cos_p * ye))
    return points


def polyline(points):
    return ' '.join(f'{x:.2f},{y:.2f}' for x, y in points)


def main():
    width = height = 1000
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}">',
        '<rect width="100%" height="100%" fill="white"/>',
        # Arrow marker used to check tangent orientation at arc ends
        '<defs><marker id="arrow" markerWidth="12" markerHeight="12" '
        'refX="9" refY="4" orient="auto">'
        '<path d="M 0 0 L 9 4 L 0 6 z" fill="darkgreen"/></marker></defs>',
    ]

    # 1+2. Rotated ellipse from two A commands + control geometry
    cx, cy, rx, ry, deg = 330, 300, 180, 90, 25
    phi = math.radians(deg)
    # Endpoints of the ellipse major axis (the arc join points)
    ex1, ey1 = cx - rx * math.cos(phi), cy - rx * math.sin(phi)
    ex2, ey2 = cx + rx * math.cos(phi), cy + rx * math.sin(phi)
    parts.append(
        f'<path d="M {ex1:.2f} {ey1:.2f} '
        f'A {rx} {ry} {deg} 1 1 {ex2:.2f} {ey2:.2f} '
        f'A {rx} {ry} {deg} 1 1 {ex1:.2f} {ey1:.2f}" '
        f'fill="none" stroke="black" stroke-width="3"/>')
    # Analytic outline of the same ellipse (red, must cover the black arcs)
    outline = [(cx + rx * math.cos(t) * math.cos(phi)
                - ry * math.sin(t) * math.sin(phi),
                cy + rx * math.cos(t) * math.sin(phi)
                + ry * math.sin(t) * math.cos(phi))
               for t in [2 * math.pi * i / 180 for i in range(181)]]
    parts.append(
        f'<polyline points="{polyline(outline)}" fill="none" '
        f'stroke="red" stroke-width="1" stroke-dasharray="4 3"/>')
    # Control points: centre and the four axis endpoints
    controls = [(cx, cy)]
    for sx, sy in ((rx, 0), (-rx, 0), (0, ry), (0, -ry)):
        controls.append((cx + sx * math.cos(phi) - sy * math.sin(phi),
                         cy + sx * math.sin(phi) + sy * math.cos(phi)))
    for px, py in controls:
        parts.append(
            f'<circle cx="{px:.2f}" cy="{py:.2f}" r="4" fill="blue"/>')
    parts.append(
        f'<line x1="{controls[1][0]:.2f}" y1="{controls[1][1]:.2f}" '
        f'x2="{controls[2][0]:.2f}" y2="{controls[2][1]:.2f}" '
        f'stroke="blue" stroke-width="0.8"/>'
        f'<line x1="{controls[3][0]:.2f}" y1="{controls[3][1]:.2f}" '
        f'x2="{controls[4][0]:.2f}" y2="{controls[4][1]:.2f}" '
        f'stroke="blue" stroke-width="0.8"/>')
    # Analytic bounding box of the rotated ellipse
    half_w = math.hypot(rx * math.cos(phi), ry * math.sin(phi))
    half_h = math.hypot(rx * math.sin(phi), ry * math.cos(phi))
    parts.append(
        f'<rect x="{cx - half_w:.2f}" y="{cy - half_h:.2f}" '
        f'width="{2 * half_w:.2f}" height="{2 * half_h:.2f}" fill="none" '
        f'stroke="grey" stroke-width="1" stroke-dasharray="6 4"/>')

    # 3. Arc fan with arrow markers + red spec tangent lines at the ends
    arcs = [
        (650, 250, 90, 45, 0, 0, 1, 850, 250),
        (650, 350, 90, 45, 40, 1, 0, 850, 350),
        (650, 450, 60, 60, -30, 0, 0, 850, 450),
        (650, 550, 30, 90, 70, 1, 1, 850, 550),
    ]
    for x1, y1, arx, ary, rot, large, sweep, x2, y2 in arcs:
        parts.append(
            f'<path d="M {x1} {y1} A {arx} {ary} {rot} {large} {sweep} '
            f'{x2} {y2}" fill="none" stroke="black" stroke-width="2" '
            f'marker-start="url(#arrow)" marker-end="url(#arrow)"/>')
        # Reference outline and tangent segments from the spec geometry
        samples = spec_arc_points(
            x1, y1, arx, ary, math.radians(rot), large, sweep, x2, y2)
        parts.append(
            f'<polyline points="{polyline(samples)}" fill="none" '
            f'stroke="red" stroke-width="0.8" stroke-dasharray="3 3"/>')
        for (pa, pb) in ((samples[0], samples[1]),
                         (samples[-1], samples[-2])):
            tx, ty = pb[0] - pa[0], pb[1] - pa[1]
            norm = math.hypot(tx, ty)
            tx, ty = tx / norm * 30, ty / norm * 30
            parts.append(
                f'<line x1="{pa[0]:.1f}" y1="{pa[1]:.1f}" '
                f'x2="{pa[0] + tx:.1f}" y2="{pa[1] + ty:.1f}" '
                f'stroke="red" stroke-width="1"/>')

    # 4. Degenerate cases (left to right):
    #    identical endpoints -> nothing; zero radius -> straight line;
    #    negative radii -> same as absolute radii (drawn side by side)
    parts.append(
        '<path d="M 120 700 A 60 60 0 0 1 120 700" fill="none" '
        'stroke="black" stroke-width="2"/>'
        '<circle cx="120" cy="700" r="3" fill="blue"/>'
        '<path d="M 220 750 A 0 60 0 0 1 380 700" fill="none" '
        'stroke="black" stroke-width="2"/>'
        '<path d="M 450 720 A -70 -45 20 0 1 620 720" fill="none" '
        'stroke="black" stroke-width="2"/>'
        '<path d="M 450 780 A 70 45 20 0 1 620 780" fill="none" '
        'stroke="black" stroke-width="2" stroke-dasharray="5 3"/>')
    # Labels
    for x, y, text in (
            (330, 560, 'ellipse: A commands (black) = spec outline (red)'),
            (750, 620, 'arcs: markers follow spec tangents (red)'),
            (120, 790, 'degenerate: none / line / abs(radii)')):
        parts.append(
            f'<text x="{x}" y="{y}" font-size="16" text-anchor="middle" '
            f'font-family="sans-serif" fill="#333">{text}</text>')

    parts.append('</svg>')
    svg = ''.join(parts)
    output = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          'arc-demo.png')
    with open(output, 'wb') as png_file:
        png_file.write(svg2png(bytestring=svg.encode('utf-8'),
                               output_width=width, output_height=height))
    print(f'demo written to {output}')


if __name__ == '__main__':
    main()
