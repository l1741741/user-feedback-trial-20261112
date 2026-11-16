#!/usr/bin/env python3
"""Raster verification of marker direction, position and scaling.

This does not compare against a pre-generated picture: it renders small,
fully controlled SVGs with CairoSVG and inspects the resulting PNG pixels to
prove that the markers on the image actually point the right way, sit at the
right vertices and scale according to the right units.

Run from the repository root::

    python work/demo/verify_markers.py
"""
import io
import sys
from math import atan2, cos, pi, sin
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from cairosvg import svg2png  # noqa: E402

ARROW_MARKER = '''
<marker id="m" markerWidth="10" markerHeight="10" refX="10" refY="5"
        markerUnits="strokeWidth" orient="{orient}" viewBox="0 0 10 10"
        overflow="visible">
  <path d="M0,0 L10,5 L0,10 z" fill="{color}"/>
</marker>'''


def render(svg):
    """Render an SVG document and return a Pillow RGBA image."""
    return Image.open(io.BytesIO(svg2png(bytestring=svg.encode()))).convert(
        'RGBA')


def colored_pixels(image, color):
    """Return the set of opaque pixel coordinates painted in ``color``."""
    target = {
        'red': (255, 0, 0),
        'green': (0, 128, 0),
        'blue': (0, 0, 255),
        'purple': (128, 0, 128),
        'navy': (0, 0, 128),
        'teal': (0, 128, 128),
    }[color]
    pixels = set()
    for y in range(image.height):
        for x in range(image.width):
            r, g, b, a = image.getpixel((x, y))
            if a and (r, g, b) == target:
                pixels.add((x, y))
    return pixels


def centroid(pixels):
    return (
        sum(x for x, _ in pixels) / len(pixels),
        sum(y for _, y in pixels) / len(pixels))


def orientation(pixels):
    """Return the direction (radians) an arrow triangle points to.

    A filled triangle's centroid lies one third of its length away from the
    wide base, toward the tip. The vector from the bounding-box center toward
    the centroid therefore points the same way as the arrow. Rasterization is
    symmetric around both axes, so this offset keeps the correct sign in all
    four cardinal and any diagonal direction.
    """
    cx, cy = centroid(pixels)
    xs = [x for x, _ in pixels]
    ys = [y for _, y in pixels]
    box_cx = (min(xs) + max(xs)) / 2
    box_cy = (min(ys) + max(ys)) / 2
    return atan2(cy - box_cy, cx - box_cx)


def angular_distance(a, b):
    return abs((a - b + pi) % (2 * pi) - pi)


FAILURES = []


def check(name, condition, detail=''):
    status = 'PASS' if condition else 'FAIL'
    print(f'[{status}] {name}' + (f' -- {detail}' if detail else ''))
    if not condition:
        FAILURES.append(name)


def svg(body, size=200):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}"
    height="{size}" viewBox="0 0 {size} {size}">
  <defs>{ARROW_MARKER.format(orient='auto', color='red')}
       {ARROW_MARKER.format(orient='auto-start-reverse', color='purple')
        .replace('id="m"', 'id="arrow-reverse"')}
  </defs>
  <rect width="{size}" height="{size}" fill="white"/>
  {body}
</svg>'''


def test_line_directions():
    # Horizontal line going right: end marker must point east (+x).
    image = render(svg(
        '<line x1="30" y1="100" x2="170" y2="100" stroke="black" '
        'stroke-width="2" marker-end="url(#m)"/>'))
    angle = orientation(colored_pixels(image, 'red'))
    check('line end marker points along travel direction',
          angular_distance(angle, 0) < radians(20),
          f'angle={angle:.2f} rad')

    # Line going left: end marker must point west.
    image = render(svg(
        '<line x1="170" y1="100" x2="30" y2="100" stroke="black" '
        'stroke-width="2" marker-end="url(#m)"/>'))
    angle = orientation(colored_pixels(image, 'red'))
    check('reversed line end marker points west',
          angular_distance(abs(angle), pi) < radians(20),
          f'angle={angle:.2f} rad')

    # Vertical line going down: end marker points south (positive y).
    # Tip on the reference point -> centroid offset points north (-pi/2).
    image = render(svg(
        '<line x1="100" y1="30" x2="100" y2="170" stroke="black" '
        'stroke-width="2" marker-end="url(#m)"/>'))
    angle = orientation(colored_pixels(image, 'red'))
    check('vertical line end marker points south',
          angular_distance(angle, -pi / 2) < radians(20),
          f'angle={angle:.2f} rad')


def test_start_reverse():
    # auto-start-reverse at the start of an eastbound line points west.
    image = render(svg(
        '<line x1="40" y1="100" x2="160" y2="100" stroke="black" '
        'stroke-width="2" marker-start="url(#arrow-reverse)"/>'))
    angle = orientation(colored_pixels(image, 'purple'))
    check('auto-start-reverse flips the start marker',
          angular_distance(abs(angle), pi) < radians(20),
          f'angle={angle:.2f} rad')

    # The same marker at the end keeps the travel direction (east).
    image = render(svg(
        '<line x1="40" y1="100" x2="160" y2="100" stroke="black" '
        'stroke-width="2" marker-end="url(#arrow-reverse)"/>'))
    angle = orientation(colored_pixels(image, 'purple'))
    check('auto-start-reverse does not flip end markers',
          angular_distance(angle, 0) < radians(20),
          f'angle={angle:.2f} rad')


def test_mid_bisector():
    # L shape: corner mid marker bisects east->south turn, points south-east.
    image = render(svg(
        '<polyline points="40,60 100,60 100,160" fill="none" stroke="black" '
        'stroke-width="2" marker-mid="url(#m)"/>'))
    pixels = colored_pixels(image, 'red')
    angle = orientation(pixels)
    # The arrow tip sits on the reference point, so the centroid offset is
    # opposite to the travel direction: south-east travel -> north-west offset.
    check('mid marker follows the corner bisector',
          angular_distance(angle, 3 * pi / 4) < radians(25),
          f'angle={angle:.2f} rad')
    # And it must be centered on the corner (100, 60).
    cx, cy = centroid(pixels)
    check('mid marker is anchored on the corner vertex',
          abs(cx - 100) <= 12 and abs(cy - 60) <= 12,
          f'centroid=({cx:.1f}, {cy:.1f})')


def test_zero_length_segments():
    # Zero-length commands between real segments must not produce NaN angles
    # or markers dropped from the image.
    image = render(svg(
        '<path d="M30,100 L30,100 L170,100 L170,100 L170,100" fill="none" '
        'stroke="black" stroke-width="2" marker-start="url(#m)" '
        'marker-mid="url(#m)" marker-end="url(#m)"/>'))
    pixels = colored_pixels(image, 'red')
    check('markers survive zero-length segments', len(pixels) > 0)
    # Every marker on the horizontal run must point east or west, never up
    # or down: the bogus atan2(0,0) angles are gone.
    directions = set()
    remaining = set(pixels)
    # Cluster pixels into connected components (each marker is one cluster).
    while remaining:
        seed = remaining.pop()
        cluster = {seed}
        frontier = [seed]
        while frontier:
            x, y = frontier.pop()
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if (nx, ny) in remaining:
                    remaining.discard((nx, ny))
                    cluster.add((nx, ny))
                    frontier.append((nx, ny))
        directions.add(round(orientation(cluster) / pi) * pi)
    check('zero-length segments keep horizontal directions',
          directions <= {0.0, pi, -pi},
          f'directions={sorted(directions)}')


def test_closed_path():
    # Closed rectangle: exactly one marker per vertex, all pointing along the
    # traversal; no duplicated marker on the closing seam.
    image = render(svg(
        '<path d="M50,50 L150,50 L150,150 L50,150 Z" fill="none" '
        'stroke="black" stroke-width="2" marker-start="url(#m)" '
        'marker-mid="url(#m)" marker-end="url(#m)"/>', size=200))
    pixels = colored_pixels(image, 'red')

    def clusters(px):
        groups = []
        remaining = set(px)
        while remaining:
            seed = remaining.pop()
            group = {seed}
            frontier = [seed]
            while frontier:
                x, y = frontier.pop()
                for nx, ny in (
                        (x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                    if (nx, ny) in remaining:
                        remaining.discard((nx, ny))
                        group.add((nx, ny))
                        frontier.append((nx, ny))
            groups.append(group)
        return groups

    groups = clusters(pixels)
    check('closed path gets one marker per vertex (4)', len(groups) == 4,
          f'clusters={len(groups)}')
    centers = [centroid(g) for g in groups]
    expected = {(50, 50), (150, 50), (150, 150), (50, 150)}
    matched = set()
    for cx, cy in centers:
        for ex, ey in expected:
            if abs(cx - ex) < 12 and abs(cy - ey) < 12:
                matched.add((ex, ey))
    check('closed path markers cover every vertex once', matched == expected,
          f'matched={sorted(matched)}')


def test_stroke_width_scaling():
    # strokeWidth units: tripling the stroke width triples the marker size.
    # The marker viewport is deliberately large and overflow visible so that
    # clipping cannot cap the measured size.
    # The marker viewport matches its viewBox and the endpoint stays well
    # inside the canvas, so neither clipping nor the image edge can cap the
    # measured size.
    marker = '''
<marker id="big" markerWidth="10" markerHeight="10" refX="10" refY="5"
        markerUnits="strokeWidth" orient="auto" viewBox="0 0 10 10"
        overflow="visible">
  <path d="M0,0 L10,5 L0,10 z" fill="red"/>
</marker>'''

    def marker_size(width):
        doc = f'''<svg xmlns="http://www.w3.org/2000/svg" width="400"
        height="200" viewBox="0 0 400 200"><defs>{marker}</defs>
        <rect width="400" height="200" fill="white"/>
        <line x1="40" y1="100" x2="200" y2="100" stroke="black"
        stroke-width="{width}" marker-end="url(#big)"/></svg>'''
        image = render(doc)
        pixels = colored_pixels(image, 'red')
        xs = [x for x, _ in pixels]
        ys = [y for _, y in pixels]
        return max(xs) - min(xs), max(ys) - min(ys)

    # Large widths keep pixel-rounding negligible relative to the marker size.
    small = marker_size(4)
    large = marker_size(8)
    ratio = large[0] / small[0]
    check('strokeWidth marker grows with stroke width',
          1.7 < ratio < 2.3, f'ratio={ratio:.2f}')
    check('strokeWidth marker scales uniformly',
          abs((large[1] / small[1]) - ratio) < 0.2)

    # userSpaceOnUse: the marker pixel size is independent of stroke width.
    userspace = '''
<marker id="u" markerWidth="16" markerHeight="16" refX="16" refY="8"
        markerUnits="userSpaceOnUse" orient="auto" viewBox="0 0 16 16"
        overflow="visible">
  <path d="M0,0 L16,8 L0,16 z" fill="navy"/>
</marker>'''

    def userspace_size(width):
        doc = f'''<svg xmlns="http://www.w3.org/2000/svg" width="200"
        height="200" viewBox="0 0 200 200"><defs>{userspace}</defs>
        <rect width="200" height="200" fill="white"/>
        <line x1="30" y1="100" x2="170" y2="100" stroke="black"
        stroke-width="{width}" marker-end="url(#u)"/></svg>'''
        image = render(doc)
        pixels = colored_pixels(image, 'navy')
        xs = [x for x, _ in pixels]
        return max(xs) - min(xs)

    size_1 = userspace_size(1)
    size_8 = userspace_size(8)
    check('userSpaceOnUse marker ignores stroke width',
          abs(size_1 - size_8) <= 2,
          f'sizes={size_1}, {size_8}')


def test_explicit_orient():
    # A numeric orient stays fixed even when the line changes direction.
    fixed = '''
<marker id="f" markerWidth="12" markerHeight="12" refX="6" refY="6"
        orient="90" viewBox="0 0 12 12" overflow="visible">
  <path d="M0,0 L12,6 L0,12 z" fill="teal"/>
</marker>'''
    doc = f'''<svg xmlns="http://www.w3.org/2000/svg" width="200"
    height="200" viewBox="0 0 200 200"><defs>{fixed}</defs>
    <rect width="200" height="200" fill="white"/>
    <line x1="30" y1="60" x2="170" y2="60" stroke="black"
    marker-end="url(#f)"/>
    <line x1="30" y1="140" x2="30" y2="140" stroke="black"
    marker-end="url(#f)"/></svg>'''
    image = render(doc)
    pixels = colored_pixels(image, 'teal')
    angle = orientation(pixels)
    # Numeric orient keeps the legacy Cairo rotation convention, preserved
    # exactly: orient=90 produces a north-pointing tip (offset -pi/2).
    check('explicit orient angle is respected',
          angular_distance(angle, -pi / 2) < radians(20),
          f'angle={angle:.2f} rad')


def _clusters(pixels):
    """Split a pixel set into 4-connected component clusters."""
    remaining = set(pixels)
    groups = []
    while remaining:
        seed = remaining.pop()
        group = {seed}
        frontier = [seed]
        while frontier:
            x, y = frontier.pop()
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if (nx, ny) in remaining:
                    remaining.discard((nx, ny))
                    group.add((nx, ny))
                    frontier.append((nx, ny))
        groups.append(group)
    return groups


def test_arc_tangents():
    # A top semicircle leaves its left endpoint heading north and reaches its
    # right endpoint heading south; both markers must follow the curvature.
    image = render(svg(
        '<path d="M40,100 A60,60 0 0 1 160,100" fill="none" stroke="black" '
        'stroke-width="2" marker-start="url(#m)" marker-end="url(#m)"/>'))
    groups = _clusters(colored_pixels(image, 'red'))
    assert len(groups) == 2
    results = {}
    for group in groups:
        center_x = sum(x for x, _ in group) / len(group)
        results['left' if center_x < 100 else 'right'] = orientation(group)
    check('arc start marker tangent points north',
          angular_distance(results['left'], pi / 2) < radians(25),
          f'angle={results["left"]:.2f}')
    check('arc end marker tangent points south',
          angular_distance(results['right'], -pi / 2) < radians(25),
          f'angle={results["right"]:.2f}')

    # Reversing the sweep flips both tangents.
    image = render(svg(
        '<path d="M40,100 A60,60 0 0 0 160,100" fill="none" stroke="black" '
        'stroke-width="2" marker-start="url(#m)" marker-end="url(#m)"/>'))
    groups = _clusters(colored_pixels(image, 'red'))
    results = {}
    for group in groups:
        center_x = sum(x for x, _ in group) / len(group)
        results['left' if center_x < 100 else 'right'] = orientation(group)
    check('reverse-sweep arc start tangent points south',
          angular_distance(results['left'], -pi / 2) < radians(25),
          f'angle={results["left"]:.2f}')
    check('reverse-sweep arc end tangent points north',
          angular_distance(results['right'], pi / 2) < radians(25),
          f'angle={results["right"]:.2f}')


def test_curve_tangents():
    image = render(svg(
        '<path d="M100,160 Q100,40 160,40" fill="none" stroke="black" '
        'stroke-width="2" marker-start="url(#m)"/>'))
    angle = orientation(colored_pixels(image, 'red'))
    check('quadratic start marker follows the real tangent',
          angular_distance(angle, pi / 2) < radians(25),
          f'angle={angle:.2f} rad')

    # Cubic curve ending with an upward tangent: same north tip.
    image = render(svg(
        '<path d="M40,160 C120,160 160,120 160,40" fill="none" '
        'stroke="black" stroke-width="2" marker-end="url(#m)"/>'))
    angle = orientation(colored_pixels(image, 'red'))
    check('cubic end marker follows the real tangent',
          angular_distance(angle, pi / 2) < radians(25),
          f'angle={angle:.2f} rad')


def radians(value):
    return value * pi / 180


def main():
    test_line_directions()
    test_start_reverse()
    test_mid_bisector()
    test_zero_length_segments()
    test_closed_path()
    test_stroke_width_scaling()
    test_explicit_orient()
    test_arc_tangents()
    test_curve_tangents()
    print()
    if FAILURES:
        print(f'{len(FAILURES)} check(s) failed: {FAILURES}')
        sys.exit(1)
    print('all marker raster checks passed')


if __name__ == '__main__':
    main()
