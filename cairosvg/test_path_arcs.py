"""
Geometry and pixel tests for elliptical arc (A/a) path commands.

The reference geometry is the endpoint-to-center conversion of the SVG 1.1
specification (appendix F.6.5), sampled as a dense polyline. Rendered PNG
pixels are then checked against this polyline:

* coverage — every sampled point of the reference arc must have a painted
  pixel nearby, so the arc cannot be truncated, flipped away or replaced
  by a straight line;
* no stray pixels — every painted pixel must be close to the reference
  arc, so flipped arcs, full-ellipse artifacts and endpoint spikes are
  detected.

Precision budget (in device pixels, 1 unit = 1 px here):

* Cairo flattens the arc with tolerance 1e-5 px (set in ``path.py``);
* the reference polyline uses 256 samples, its chordal error is < 1e-3 px;
* antialiasing bleeds about 1 px around the ideal stroke;
* the stroke is 4 px wide, so its half-width is 2 px.

The tolerance used for both checks is therefore half stroke width + AA
bleed + a safety margin: 2 + 1 + 0.5 = 3.5 px, three orders of magnitude
above the numerical error of the implementation itself.

"""

import io
import math

import cairocffi as cairo
import pytest

from . import svg2png

CANVAS = 300
STROKE = 4
TOLERANCE = STROKE / 2 + 1.5

# Offsets of all pixels within TOLERANCE of a point
OFFSETS = [
    (dx, dy)
    for dx in range(-4, 5) for dy in range(-4, 5)
    if math.hypot(dx, dy) <= TOLERANCE]


def reference_arc(x1, y1, x2, y2, rx, ry, rotation, large, sweep, n=256):
    """Sample the arc defined by SVG 1.1 F.6.5 as a polyline."""
    rx, ry = abs(rx), abs(ry)
    if not rx or not ry or (x1 == x2 and y1 == y2):
        return [(x1, y1), (x2, y2)]
    phi = math.radians(rotation)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    x1p = cos_phi * dx + sin_phi * dy
    y1p = -sin_phi * dx + cos_phi * dy

    # F.6.5.1: scale radii up if they are too small for the chord
    lam = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
    if lam > 1:
        scale = math.sqrt(lam)
        rx, ry = rx * scale, ry * scale

    # F.6.5.2: center in the rotated/translated frame
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    root = math.sqrt(max(0.0, num / den))
    if large == sweep:
        root = -root
    cxp = root * rx * y1p / ry
    cyp = -root * ry * x1p / rx

    # F.6.5.3: center in the original frame
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2

    # F.6.5.4/5: start angle and sweep angle
    def angle(ux, uy, vx, vy):
        norm = math.hypot(ux, uy) * math.hypot(vx, vy)
        cosine = max(-1.0, min(1.0, (ux * vx + uy * vy) / norm))
        result = math.acos(cosine)
        return -result if ux * vy - uy * vx < 0 else result

    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    delta = angle(
        (x1p - cxp) / rx, (y1p - cyp) / ry,
        (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi

    return [
        (cos_phi * rx * math.cos(theta1 + delta * i / n)
         - sin_phi * ry * math.sin(theta1 + delta * i / n) + cx,
         sin_phi * rx * math.cos(theta1 + delta * i / n)
         + cos_phi * ry * math.sin(theta1 + delta * i / n) + cy)
        for i in range(n + 1)]


def render(d, canvas=CANVAS, stroke=STROKE):
    """Render a single stroked path, return PNG bytes."""
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{canvas}" height="{canvas}">'
        f'<path d="{d}" fill="none" stroke="black" '
        f'stroke-width="{stroke}"/></svg>')
    return svg2png(svg)


def painted_pixels(png_bytes):
    """Return the set of (x, y) pixels whose alpha is at least 1/8."""
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))
    width, height, stride = (
        surface.get_width(), surface.get_height(), surface.get_stride())
    data = bytes(surface.get_data())
    return {
        (x, y)
        for y in range(height) for x in range(width)
        if data[y * stride + x * 4 + 3] >= 32}


def dilate(points):
    """Return all integer pixels within TOLERANCE of the polyline."""
    pixels = set()
    for x, y in points:
        for dx, dy in OFFSETS:
            pixels.add((round(x) + dx, round(y) + dy))
    return pixels


def densify(points, step=2.0):
    """Interpolate the polyline so that consecutive points are close."""
    dense = []
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        length = math.hypot(x2 - x1, y2 - y1)
        count = max(1, math.ceil(length / step))
        dense.extend(
            (x1 + (x2 - x1) * i / count, y1 + (y2 - y1) * i / count)
            for i in range(count))
    dense.append(points[-1])
    return dense


def assert_arc_matches(pixels, reference):
    """Check coverage (no missing arc parts) and purity (no stray ink)."""
    reference = densify(reference)
    allowed = dilate(reference)
    missing = [
        point for i, point in enumerate(reference) if i % 4 == 0
        if not any(
            (round(point[0]) + dx, round(point[1]) + dy) in pixels
            for dx, dy in OFFSETS)]
    stray = [pixel for pixel in pixels if pixel not in allowed]
    assert not missing, f'arc not drawn near {missing[:3]}'
    assert not stray, f'unexpected ink at {stray[:3]}'


X1, Y1, X2, Y2 = 120, 150, 180, 150


@pytest.mark.parametrize('rx', [25, -35])
@pytest.mark.parametrize('ry', [15, -45])
@pytest.mark.parametrize('rotation', [0, 30, -45, 75])
@pytest.mark.parametrize('large', [0, 1])
@pytest.mark.parametrize('sweep', [0, 1])
def test_arc_geometry(rx, ry, rotation, large, sweep):
    """Rendered pixels match the spec arc for all flag/radius/rotation
    combinations, including negative radii (taken as absolute values)."""
    d = f'M {X1} {Y1} A {rx} {ry} {rotation} {large} {sweep} {X2} {Y2}'
    reference = reference_arc(X1, Y1, X2, Y2, rx, ry, rotation, large, sweep)
    assert_arc_matches(painted_pixels(render(d)), reference)


@pytest.mark.parametrize('letter', ['a', 'A'])
def test_arc_endpoints_are_exact(letter):
    """The stroke covers both arc endpoints (no gap, no overshoot)."""
    if letter == 'a':
        d = f'M {X1} {Y1} a 30 20 40 0 1 {X2 - X1} {Y2 - Y1}'
    else:
        d = f'M {X1} {Y1} A 30 20 40 0 1 {X2} {Y2}'
    pixels = painted_pixels(render(d))
    for x, y in ((X1, Y1), (X2, Y2)):
        assert any(
            (x + dx, y + dy) in pixels for dx, dy in OFFSETS), (x, y)


def test_negative_radii_equal_absolute_values():
    """A -rx -ry renders exactly like A rx ry (SVG 1.1 F.6.2)."""
    positive = render(f'M {X1} {Y1} A 25 15 30 0 1 {X2} {Y2}')
    negative = render(f'M {X1} {Y1} A -25 -15 30 0 1 {X2} {Y2}')
    mixed = render(f'M {X1} {Y1} A -25 15 30 0 1 {X2} {Y2}')
    assert negative == positive
    assert mixed == positive


def test_relative_arc_equals_absolute():
    """Relative ``a`` renders exactly like the equivalent absolute ``A``."""
    absolute = render(f'M {X1} {Y1} A 30 20 15 1 0 {X2} {Y2}')
    relative = render(f'M {X1} {Y1} a 30 20 15 1 0 {X2 - X1} {Y2 - Y1}')
    assert relative == absolute


def test_identical_endpoints_omit_arc():
    """Same start and end point: the arc is omitted (SVG 1.1 F.6.2) and
    the rest of the path is left intact."""
    with_arc = render(f'M 50 150 A 25 15 0 0 1 50 150 L 250 150')
    without_arc = render('M 50 150 L 250 150')
    assert with_arc == without_arc
    # Same, with relative coordinates and a following relative segment
    with_arc = render('M 50 150 a 25 15 0 1 1 0 0 l 200 0')
    assert with_arc == without_arc


def test_zero_radius_is_straight_line():
    """A zero radius turns the arc into a line to the end point."""
    for d in ('M 50 150 A 0 15 0 0 1 250 150',
              'M 50 150 A 25 0 0 1 1 250 150',
              'M 50 150 a 0 0 30 0 0 200 0'):
        assert render(d) == render('M 50 150 L 250 150')


def test_insufficient_radii_are_scaled_up():
    """Radii too small for the chord are scaled uniformly (F.6.5.1)."""
    # Chord is 60 long: rx=25 is too small and must grow to 30+
    d = f'M {X1} {Y1} A 25 40 0 0 1 {X2} {Y2}'
    reference = reference_arc(X1, Y1, X2, Y2, 25, 40, 0, 0, 1)
    assert_arc_matches(painted_pixels(render(d)), reference)


def test_consecutive_arcs():
    """Consecutive arcs (with omitted letters) join exactly."""
    d = (f'M 40 150 A 30 20 0 0 1 100 150 '
         f'A 30 20 0 1 1 160 150 '
         f'a 30 20 0 0 0 60 0')
    reference = (
        reference_arc(40, 150, 100, 150, 30, 20, 0, 0, 1)
        + reference_arc(100, 150, 160, 150, 30, 20, 0, 1, 1)
        + reference_arc(160, 150, 220, 150, 30, 20, 0, 0, 0))
    assert_arc_matches(painted_pixels(render(d)), reference)


def test_degenerate_arc_does_not_break_following_path():
    """Degenerate arcs (zero radius, identical endpoints) must not disturb
    the current point or the parsing of the remaining commands."""
    d = ('M 40 150 '
         'a 0 20 0 0 1 40 0 '        # zero radius: line to (80, 150)
         'a 30 20 0 0 1 0 0 '         # identical endpoints: omitted
         'a 30 20 0 0 1 40 0 '        # real arc to (120, 150)
         'l 0 -40')                   # line to (120, 110)
    reference = (
        [(40, 150), (80, 150)]
        + reference_arc(80, 150, 120, 150, 30, 20, 0, 0, 1)
        + [(120, 150), (120, 110)])
    assert_arc_matches(painted_pixels(render(d)), reference)


def test_stroke_is_continuous_across_joints():
    """Arc/line and arc/arc joints leave no gap in the stroke."""
    for d in (
            'M 60 150 A 40 25 20 0 1 200 150 L 200 100',
            'M 40 150 A 30 20 0 0 1 120 150 A 30 20 0 1 1 200 150'):
        pixels = painted_pixels(render(d, stroke=6))
        # Flood fill from any painted pixel: everything must be connected
        seen = {next(iter(pixels))}
        stack = list(seen)
        while stack:
            x, y = stack.pop()
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbour = (x + dx, y + dy)
                    if neighbour in pixels and neighbour not in seen:
                        seen.add(neighbour)
                        stack.append(neighbour)
        assert seen == pixels, f'gap in stroke for {d}'


def test_rendering_is_deterministic():
    """The same input always produces byte-identical output."""
    d = ('M 20 150 A 45 25 30 0 1 120 140 a -30 -15 -20 1 0 60 10 '
         'A 25 25 0 1 1 260 150 a 0 10 0 0 1 10 0 a 5 5 0 0 1 0 0 z')
    assert render(d) == render(d)
