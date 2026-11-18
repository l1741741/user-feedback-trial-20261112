"""
Elliptical arc (A/a path commands) geometry test suite.

The reference implementation is the endpoint-to-center conversion from
SVG 1.1 appendix F.6.5 (also SVG 2 §9.5.1): arcs are sampled analytically
and the rendered PNG is checked against the samples.

Precision tolerance
-------------------
All checks are done on a 900×900 px canvas at 96 dpi with a 2 px stroke:

* point-on-curve checks accept ink within a radius of 2 device pixels
  (≈1 px geometry flattening at tolerance 1e-5 + 1 px stroke half-width
  + anti-aliasing fringe);
* "no ink" checks require a clearance of 1 px around points that are at
  least 8 px away from the true curve, so flag/centre flips (which move
  the arc by tens of pixels) are always detected;
* tangent (marker orientation) checks probe 12 px away from the vertex,
  giving an angular tolerance of about ±5° (12·tan 5° ≈ 1 px);
* exact-equality checks (relative vs absolute arcs, negative vs absolute
  radii, repeated exports) compare PNG bytes, which is stricter than any
  geometric tolerance: cairo is deterministic for identical input.

"""

import io
import math

import cairocffi as cairo
import pytest

from . import svg2png

CANVAS = 900


def spec_arc_points(x1, y1, rx, ry, phi, large, sweep, x2, y2, n=96):
    """Sample the arc defined by SVG 1.1 F.6.5 endpoint parametrization."""
    rx, ry = abs(rx), abs(ry)
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [(x1, y1), (x2, y2)]
    dx, dy = (x1 - x2) / 2, (y1 - y2) / 2
    cos_p, sin_p = math.cos(phi), math.sin(phi)
    x1p = cos_p * dx + sin_p * dy
    y1p = -sin_p * dx + cos_p * dy
    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:  # radii too small: scale them up uniformly
        scale = math.sqrt(lam)
        rx, ry = scale * rx, scale * ry
    num = rx ** 2 * ry ** 2 - rx ** 2 * y1p ** 2 - ry ** 2 * x1p ** 2
    den = rx ** 2 * y1p ** 2 + ry ** 2 * x1p ** 2
    coef = math.sqrt(max(0.0, num / den))
    if large == sweep:
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_p * cxp - sin_p * cyp + (x1 + x2) / 2
    cy = sin_p * cxp + cos_p * cyp + (y1 + y2) / 2

    def vector_angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        det = ux * vy - uy * vx
        return math.atan2(det, dot)

    theta1 = vector_angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = vector_angle(
        (x1p - cxp) / rx, (y1p - cyp) / ry,
        (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi
    points = []
    for i in range(n + 1):
        theta = theta1 + dtheta * i / n
        xe, ye = rx * math.cos(theta), ry * math.sin(theta)
        points.append((
            cx + cos_p * xe - sin_p * ye,
            cy + sin_p * xe + cos_p * ye))
    return points


def spec_arc_tangent(x1, y1, rx, ry, phi, large, sweep, x2, y2, at_end):
    """Return the unit tangent of the arc at its start or end point."""
    points = spec_arc_points(x1, y1, rx, ry, phi, large, sweep, x2, y2, n=720)
    if at_end:
        (xa, ya), (xb, yb) = points[-2], points[-1]
    else:
        (xa, ya), (xb, yb) = points[0], points[1]
    length = math.hypot(xb - xa, yb - ya)
    return (xb - xa) / length, (yb - ya) / length


class Image:
    """Read pixel alpha from a rendered PNG."""

    def __init__(self, png_bytes):
        surface = cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))
        self.width = surface.get_width()
        self.height = surface.get_height()
        self.stride = surface.get_stride()
        self.data = bytes(surface.get_data())

    def alpha(self, x, y):
        x, y = int(round(x)), int(round(y))
        if 0 <= x < self.width and 0 <= y < self.height:
            return self.data[y * self.stride + x * 4 + 3]
        return 0

    def inked(self, x, y, radius=2):
        for yy in range(int(round(y)) - radius, int(round(y)) + radius + 1):
            for xx in range(int(round(x)) - radius, int(round(x)) + radius + 1):
                if self.alpha(xx, yy) > 40:
                    return True
        return False

    def any_ink(self):
        return any(
            self.data[y * self.stride + x * 4 + 3] > 40
            for y in range(self.height) for x in range(self.width))


def render_path(d, extra='', canvas=CANVAS, stroke_width=2):
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{canvas}" height="{canvas}">{extra}'
        f'<path d="{d}" fill="none" stroke="black" '
        f'stroke-width="{stroke_width}"/></svg>')
    return svg2png(bytestring=svg.encode('utf-8'),
                   output_width=canvas, output_height=canvas)


ARC_CASES = [
    # x1, y1, rx, ry, rotation(deg), large, sweep, x2, y2
    (350, 400, 60, 40, 0, 0, 0, 550, 400),
    (350, 400, 60, 40, 0, 0, 1, 550, 400),
    (350, 400, 60, 40, 0, 1, 0, 550, 400),
    (350, 400, 60, 40, 0, 1, 1, 550, 400),
    (350, 400, 60, 40, 30, 0, 1, 550, 400),
    (350, 400, 60, 40, 30, 1, 0, 550, 400),
    (350, 400, 60, 40, 90, 0, 1, 550, 400),
    (350, 400, 60, 40, 90, 1, 0, 550, 400),
    (350, 400, 60, 40, -45, 0, 1, 550, 400),
    (350, 400, 60, 40, -45, 1, 0, 550, 400),
    (350, 400, 30, 80, 15, 1, 1, 550, 450),
    (350, 400, 30, 80, 15, 0, 0, 550, 450),
    (350, 400, 20, 20, 0, 0, 1, 550, 400),    # radii too small: scaled up
    (350, 400, 20, 20, 45, 1, 0, 550, 400),   # radii too small + rotation
    (350, 350, 90, 50, 60, 0, 1, 590, 590),
    (350, 350, 90, 50, 60, 1, 0, 590, 590),
    (350, 350, 90, 50, 120, 0, 0, 590, 590),
    (350, 350, 90, 50, 120, 1, 1, 590, 590),
    (350, 350, 150, 150, 0, 1, 1, 351, 351),  # almost full circle
    (350, 350, 5, 200, 75, 0, 1, 500, 300),   # extreme aspect ratio
]


@pytest.mark.parametrize(
    'x1,y1,rx,ry,degrees,large,sweep,x2,y2', ARC_CASES)
def test_arc_matches_spec_geometry(x1, y1, rx, ry, degrees, large, sweep,
                                   x2, y2):
    """Every sample of the spec curve must land on rendered ink, and the
    complementary arc (opposite large-arc flag) must stay empty: this
    catches flipped centres and flipped sweep directions."""
    image = Image(render_path(
        f'M {x1} {y1} A {rx} {ry} {degrees} {large} {sweep} {x2} {y2}'))
    phi = math.radians(degrees)
    points = spec_arc_points(x1, y1, rx, ry, phi, large, sweep, x2, y2)
    for point in points:
        assert image.inked(*point), f'arc sample {point} not rendered'
    other = spec_arc_points(x1, y1, rx, ry, phi, not large, sweep, x2, y2)
    mid_other = other[len(other) // 2]
    clearance = min(
        math.hypot(mid_other[0] - px, mid_other[1] - py) for px, py in points)
    if clearance > 8:
        assert not image.inked(*mid_other, radius=1), (
            f'ink on complementary arc at {mid_other}')


def test_arc_endpoints_exact():
    """Arc endpoints must join the neighbouring segments exactly."""
    image = Image(render_path('M 350 400 A 60 40 30 0 1 550 400'))
    assert image.inked(350, 400, radius=1)
    assert image.inked(550, 400, radius=1)


def test_identical_endpoints_draw_nothing():
    """F.6.4: arcs with identical endpoints are omitted entirely."""
    for flags in ('0 0', '0 1', '1 0', '1 1'):
        image = Image(render_path(f'M 100 100 A 50 50 0 {flags} 100 100'))
        assert not image.any_ink()
    image = Image(render_path('M 100 100 a 50 50 0 0 1 0 0'))
    assert not image.any_ink()


def test_degenerate_arc_does_not_break_following_path():
    """A degenerate arc must not disturb the current point or the rest of
    the path data."""
    image = Image(render_path(
        'M 100 100 A 50 50 0 0 1 100 100 L 300 100 L 300 300'))
    assert image.inked(200, 100)   # horizontal line after the dead arc
    assert image.inked(300, 200)   # vertical line after that
    assert not image.inked(100, 200, radius=2)


def test_zero_radius_is_straight_line():
    """F.6.2: rx=0 or ry=0 is rendered as a straight line."""
    for radii in ('0 50', '50 0', '0 0'):
        image = Image(render_path(f'M 100 100 A {radii} 0 0 1 300 300'))
        assert image.inked(200, 200)
        assert not image.inked(200, 100, radius=3)
        assert not image.inked(100, 200, radius=3)


def test_negative_radius_uses_absolute_value():
    """SVG 2: negative radii are replaced by their absolute values (they
    must not mirror the arc or flip its sweep direction)."""
    negative = render_path('M 350 400 A -60 -40 30 0 1 550 400')
    positive = render_path('M 350 400 A 60 40 30 0 1 550 400')
    assert negative == positive


def test_relative_arc_matches_absolute():
    """An ``a`` command must render exactly like its ``A`` equivalent."""
    relative = render_path('M 350 400 a 60 40 30 0 1 200 0')
    absolute = render_path('M 350 400 A 60 40 30 0 1 550 400')
    assert relative == absolute


def test_consecutive_arcs_join_without_spike():
    """Chained arcs share their endpoints; no stray line may appear at the
    joint, and the following segment starts at the arc endpoint."""
    image = Image(render_path(
        'M 300 450 A 50 50 0 0 1 400 450 A 50 50 0 0 1 500 450 L 500 550'))
    assert image.inked(400, 450, radius=1)      # joint point is on the path
    assert not image.inked(400, 350, radius=2)  # no spike above the joint
    assert not image.inked(400, 550, radius=2)  # no spike below the joint
    assert image.inked(500, 500)                # line continues from arc end


def test_arc_after_close_path():
    """Arcs following Z start at the sub-path start point."""
    image = Image(render_path(
        'M 100 100 L 200 100 L 200 200 Z A 40 40 0 0 1 300 100'))
    assert image.inked(100, 100, radius=1)
    assert image.inked(300, 100, radius=1)


MARKER = (
    '<defs><marker id="arrow" markerWidth="20" markerHeight="20" '
    'refX="10" refY="5" orient="auto">'
    '<path d="M 0 0 L 10 5 L 0 7 z" fill="black"/></marker></defs>')

MARKER_CASES = [
    # x1, y1, rx, ry, rotation(deg), large, sweep, x2, y2
    (400, 500, 100, 100, 0, 0, 1, 500, 400),
    (400, 500, 100, 100, 0, 1, 0, 500, 400),
    (350, 400, 60, 40, 30, 0, 1, 550, 400),
    (350, 400, 60, 40, 30, 1, 0, 550, 400),
    (350, 400, 80, 30, -60, 0, 0, 500, 500),
    (350, 400, 80, 30, -60, 1, 1, 500, 500),
]


@pytest.mark.parametrize(
    'x1,y1,rx,ry,degrees,large,sweep,x2,y2', MARKER_CASES)
def test_arc_marker_follows_tangent(x1, y1, rx, ry, degrees, large, sweep,
                                    x2, y2):
    """marker-end orient="auto" must align with the arc tangent: the arrow
    body (12 px behind the tip, refX puts the tip on the vertex) is inked
    along the spec tangent, and empty 12 px off the normal direction."""
    d = f'M {x1} {y1} A {rx} {ry} {degrees} {large} {sweep} {x2} {y2}'
    image = Image(render_path(d, extra=MARKER, stroke_width=1))
    tangent = spec_arc_tangent(
        x1, y1, rx, ry, math.radians(degrees), large, sweep, x2, y2,
        at_end=True)
    behind = (x2 - 12 * tangent[0], y2 - 12 * tangent[1])
    sideways = (x2 - 12 * tangent[1], y2 + 12 * tangent[0])
    assert image.inked(*behind, radius=2), (
        f'arrow body not on tangent at {behind}')
    assert not image.inked(*sideways, radius=1), (
        f'unexpected ink off tangent at {sideways}')


def test_repeated_export_is_deterministic():
    """The same input exported twice gives byte-identical images."""
    d = ('M 120 450 A 60 40 30 0 1 320 450 a 30 80 -15 1 0 150 60 '
         'A 20 20 0 0 0 700 300 A 50 50 0 1 1 120 450 Z')
    assert render_path(d) == render_path(d)


def test_engineering_drawing_regression():
    """Path regression: a rotated/scaled engineering-drawing-like group of
    arcs keeps its endpoints, orientation and ink bounding box stable."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="900" height="900">'
        '<g transform="translate(450 450) rotate(15) scale(1.5 0.75)">'
        '<path d="M -200 0 A 200 120 0 1 1 200 0 A 200 120 0 1 1 -200 0 '
        'M -100 -60 A 40 40 0 0 1 -20 -60 '
        'M 20 60 A 40 40 0 1 0 100 60" '
        'fill="none" stroke="black" stroke-width="2"/></g></svg>')
    image = Image(svg2png(bytestring=svg.encode('utf-8'),
                          output_width=900, output_height=900))
    # Ink bounding box of the transformed ellipse: centre (450,450),
    # radii (1.5*200, 0.75*120) = (300, 90) rotated by 15°. The extreme
    # points of a rotated ellipse are not on the centre axes, compute them
    # from the ellipse parametrization.
    phi = math.radians(15)
    a, b = 300.0, 90.0

    def ellipse_point(theta):
        xe, ye = a * math.cos(theta), b * math.sin(theta)
        return (450 + xe * math.cos(phi) - ye * math.sin(phi),
                450 + xe * math.sin(phi) + ye * math.cos(phi))

    extremes = [
        ellipse_point(math.atan2(-b * math.sin(phi), a * math.cos(phi))),
        ellipse_point(math.atan2(b * math.sin(phi), -a * math.cos(phi))),
        ellipse_point(math.atan2(b * math.cos(phi), a * math.sin(phi))),
        ellipse_point(math.atan2(-b * math.cos(phi), -a * math.sin(phi))),
    ]
    for point in extremes:
        assert image.inked(*point, radius=3), f'boundary {point} not reached'
    # Nothing outside the bounding box (plus stroke/AA margin)
    half_w = math.hypot(a * math.cos(phi), b * math.sin(phi))
    half_h = math.hypot(a * math.sin(phi), b * math.cos(phi))
    margin = 4
    assert not image.inked(450 - half_w - margin - 2, 450, radius=1)
    assert not image.inked(450, 450 - half_h - margin - 2, radius=1)
