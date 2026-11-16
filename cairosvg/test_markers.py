"""
Regression tests for SVG marker rendering.

These tests render small controlled SVG documents and inspect the resulting
PNG pixels or the structured marker placements: they never compare against a
pre-generated image, so a change is only accepted when the markers really
point the right way, sit on the right vertices and scale with the right
units.

"""

import io
from math import atan2, pi, sin

import pytest

from . import svg2png
from .path import (
    CLOSE_PATH, _parse_marker_vertices, cubic_angles, marker_mid_angle,
    quadratic_angles)

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

requires_pillow = pytest.mark.skipif(
    Image is None, reason='Pillow is required to inspect rendered pixels')


# A directional arrow whose tip (local +x) sits on the reference point.
ARROW_MARKER = '''
<marker id="m" markerWidth="10" markerHeight="10" refX="10" refY="5"
        markerUnits="strokeWidth" orient="{orient}" viewBox="0 0 10 10"
        overflow="visible">
  <path d="M0,0 L10,5 L0,10 z" fill="red"/>
</marker>'''


def _render(body, size=200):
    document = f'''<svg xmlns="http://www.w3.org/2000/svg" width="{size}"
    height="{size}" viewBox="0 0 {size} {size}">
  <defs>{ARROW_MARKER.format(orient='auto')}
       {ARROW_MARKER.format(orient='auto-start-reverse')
        .replace('id="m"', 'id="m-reverse"')}</defs>
  <rect width="{size}" height="{size}" fill="white"/>
  {body}
</svg>'''
    return Image.open(io.BytesIO(
        svg2png(bytestring=document.encode()))).convert('RGBA')


def _red_pixels(image):
    return {
        (x, y)
        for y in range(image.height)
        for x in range(image.width)
        if image.getpixel((x, y))[:3] == (255, 0, 0)}


def _centroid(pixels):
    return (
        sum(x for x, _ in pixels) / len(pixels),
        sum(y for _, y in pixels) / len(pixels))


def _orientation(pixels):
    """Return the rendered pointing direction (radians) of an arrow.

    The tip of a filled triangle lies on the reference point, so the
    centroid is offset toward the wide base, opposite to the travel
    direction. The returned angle is the centroid offset measured inside the
    colored bounding box.
    """
    cx, cy = _centroid(pixels)
    xs = [x for x, _ in pixels]
    ys = [y for _, y in pixels]
    return atan2(
        cy - (min(ys) + max(ys)) / 2,
        cx - (min(xs) + max(xs)) / 2)


def _clusters(pixels):
    remaining = set(pixels)
    groups = []
    while remaining:
        seed = remaining.pop()
        group = {seed}
        frontier = [seed]
        while frontier:
            x, y = frontier.pop()
            for neighbour in (
                    (x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if neighbour in remaining:
                    remaining.discard(neighbour)
                    group.add(neighbour)
                    frontier.append(neighbour)
        groups.append(group)
    return groups


def _angle_distance(a, b):
    return abs((a - b + pi) % (2 * pi) - pi)


# ---------------------------------------------------------------------------
# Geometry helpers (pure functions, no Cairo rasterization required)
# ---------------------------------------------------------------------------

def test_quadratic_angles_follow_control_points():
    # Start tangent points toward the control point, end tangent away from it.
    start, end = quadratic_angles(0, 10, 0, 0, 10, 0)
    assert start == pytest.approx(-pi / 2)
    assert end == pytest.approx(0)


def test_cubic_angles_use_endpoint_derivatives():
    # The old implementation derived the start tangent from the two control
    # points instead of the start point; check the corrected formula.
    # P0(0,0) P1(10,0): start tangent east. P2(10,10) P3(0,10): end west.
    start, end = cubic_angles(0, 0, 10, 0, 10, 10, 0, 10)
    assert start == pytest.approx(0)
    assert end == pytest.approx(pi)


def test_cubic_angles_fallback_on_zero_derivative():
    # Coincident start point and first control point: fall back rather than
    # returning an undefined angle.
    start, _ = cubic_angles(0, 0, 0, 0, 10, 0, 10, 10)
    assert start == pytest.approx(0)


def test_mid_angle_collinear_and_reversal():
    assert marker_mid_angle(0.3, 0.3) == pytest.approx(0.3)
    # Exactly opposite tangents have no bisector: keep the incoming one.
    assert marker_mid_angle(1.0, 1.0 + pi) == pytest.approx(1.0)


def test_parser_open_subpath_skips_zero_length_segments():
    vertices = [
        (0.0, 0.0), (0.0, 0.0),          # zero-length segment
        (0.0, 0.0), (0.0, 0.0),          # another one
        (10.0, 0.0), (pi / 2, pi / 2),   # turn south
        (10.0, 10.0), (pi, pi),          # go west
        (0.0, 10.0),
    ]
    subpaths = _parse_marker_vertices(vertices)
    assert len(subpaths) == 1
    closed, placements = subpaths[0]
    assert closed is False
    # Start and end use the tangents of the surrounding real segments; the
    # two real corners each get a bisector mid marker. Zero-length vertices
    # add no extra placement and introduce no undefined orientation.
    by_position = {}
    for point, angle, position in placements:
        by_position.setdefault(position, []).append((point, angle))
    assert [p for _, _, p in placements] == [
        'start', 'mid', 'mid', 'end']
    assert by_position['start'][0][0] == (0.0, 0.0)
    assert by_position['start'][0][1] == pytest.approx(0.0)
    assert by_position['end'][0][0] == (0.0, 10.0)
    assert by_position['end'][0][1] == pytest.approx(pi)
    # The first corner bisects east (0) and south (pi/2).
    corner_points = [point for point, _ in by_position['mid']]
    assert (10.0, 0.0) in corner_points
    assert (10.0, 10.0) in corner_points


def test_parser_closed_subpath_has_only_mid_markers():
    vertices = [
        (0.0, 0.0), (0.0, 0.0),
        (10.0, 0.0), (pi / 2, pi / 2),
        (10.0, 10.0), (pi, pi),
        (0.0, 10.0), (-pi / 2, -pi / 2),
        CLOSE_PATH,
    ]
    subpaths = _parse_marker_vertices(vertices)
    closed, placements = subpaths[0]
    assert closed is True
    # One marker per vertex, all mid: the closing seam is not duplicated.
    assert len(placements) == 4
    assert all(position == 'mid' for _, _, position in placements)
    assert {point for point, _, _ in placements} == {
        (0.0, 0.0), (10.0, 0.0), (10.0, 10.0), (0.0, 10.0)}


def test_parser_degenerate_subpaths():
    # An open subpath made only of zero-length segments keeps both endpoints
    # with a null orientation instead of crashing.
    open_placements = _parse_marker_vertices(
        [(5.0, 5.0), (0.0, 0.0), (5.0, 5.0)])[0][1]
    assert [position for _, _, position in open_placements] == [
        'start', 'end']

    # A closed degenerate subpath collapses onto a single mid position.
    closed, closed_placements = _parse_marker_vertices(
        [(5.0, 5.0), (0.0, 0.0), (5.0, 5.0), CLOSE_PATH])[0]
    assert closed is True
    assert len(closed_placements) == 1
    assert closed_placements[0][2] == 'mid'


def test_parser_multiple_subpaths():
    vertices = [
        (0.0, 0.0), (0.0, 0.0), (5.0, 0.0),
        None,
        (20.0, 20.0), (pi / 2, pi / 2), (20.0, 30.0),
    ]
    subpaths = _parse_marker_vertices(vertices)
    assert len(subpaths) == 2
    assert [positions for _, placements in subpaths
            for positions in [[p[2] for p in placements]]] == [
        ['start', 'end'], ['start', 'end']]


# ---------------------------------------------------------------------------
# Rasterized behavior
# ---------------------------------------------------------------------------

@requires_pillow
def test_line_marker_follows_travel_direction():
    # Eastbound line: the rendered arrow points along +x, giving a centroid
    # offset toward the base of zero radians.
    image = _render(
        '<line x1="30" y1="100" x2="170" y2="100" stroke="black" '
        'stroke-width="2" marker-end="url(#m)"/>')
    assert _angle_distance(_orientation(_red_pixels(image)), 0) < 0.4

    # Southbound line: centroid offset points north (-pi/2).
    image = _render(
        '<line x1="100" y1="30" x2="100" y2="170" stroke="black" '
        'stroke-width="2" marker-end="url(#m)"/>')
    assert _angle_distance(_orientation(_red_pixels(image)), -pi / 2) < 0.4


@requires_pillow
def test_start_reverse_marker():
    # The start marker of an eastbound line is flipped to point west, so its
    # centroid offset is pi (opposite to the non-reversed end marker).
    image = _render(
        '<line x1="40" y1="100" x2="160" y2="100" stroke="black" '
        'stroke-width="2" marker-start="url(#m-reverse)"/>')
    assert _angle_distance(_orientation(_red_pixels(image)), pi) < 0.4


@requires_pillow
def test_closed_path_one_marker_per_vertex():
    image = _render(
        '<path d="M50,50 L150,50 L150,150 L50,150 Z" fill="none" '
        'stroke="black" stroke-width="2" marker-start="url(#m)" '
        'marker-mid="url(#m)" marker-end="url(#m)"/>')
    groups = _clusters(_red_pixels(image))
    assert len(groups) == 4
    centers = {_centroid(group) for group in groups}
    for expected in ((50, 50), (150, 50), (150, 150), (50, 150)):
        assert any(
            abs(cx - expected[0]) < 12 and abs(cy - expected[1]) < 12
            for cx, cy in centers)


@requires_pillow
def test_zero_length_segments_render_horizontal_markers():
    image = _render(
        '<path d="M30,100 L30,100 L170,100 L170,100" fill="none" '
        'stroke="black" stroke-width="2" marker-start="url(#m)" '
        'marker-mid="url(#m)" marker-end="url(#m)"/>')
    for group in _clusters(_red_pixels(image)):
        angle = _orientation(group)
        # Every marker stays horizontal: no NaN-driven vertical orientation.
        assert abs(sin(angle)) < 0.3


@requires_pillow
def test_stroke_width_scales_marker():
    def marker_size(width):
        document = f'''<svg xmlns="http://www.w3.org/2000/svg"
        width="300" height="200" viewBox="0 0 300 200">
        <defs>{ARROW_MARKER.format(orient='auto')}</defs>
        <rect width="300" height="200" fill="white"/>
        <line x1="40" y1="100" x2="180" y2="100" stroke="black"
        stroke-width="{width}" marker-end="url(#m)"/></svg>'''
        image = Image.open(io.BytesIO(
            svg2png(bytestring=document.encode()))).convert('RGBA')
        pixels = _red_pixels(image)
        xs = [x for x, _ in pixels]
        return max(xs) - min(xs)

    # Doubling the stroke width roughly doubles the rendered marker size.
    ratio = marker_size(6) / marker_size(3)
    assert 1.7 < ratio < 2.3


@requires_pillow
def test_userspace_on_use_ignores_stroke_width():
    marker = '''
<marker id="u" markerWidth="16" markerHeight="16" refX="16" refY="8"
        markerUnits="userSpaceOnUse" orient="auto" viewBox="0 0 16 16"
        overflow="visible">
  <path d="M0,0 L16,8 L0,16 z" fill="navy"/>
</marker>'''

    def size(width):
        document = f'''<svg xmlns="http://www.w3.org/2000/svg"
        width="200" height="200" viewBox="0 0 200 200"><defs>{marker}</defs>
        <rect width="200" height="200" fill="white"/>
        <line x1="30" y1="100" x2="170" y2="100" stroke="black"
        stroke-width="{width}" marker-end="url(#u)"/></svg>'''
        image = Image.open(io.BytesIO(
            svg2png(bytestring=document.encode()))).convert('RGBA')
        navy = {
            (x, y)
            for y in range(image.height)
            for x in range(image.width)
            if image.getpixel((x, y))[:3] == (0, 0, 128)}
        xs = [x for x, _ in navy]
        return max(xs) - min(xs)

    assert abs(size(1) - size(8)) <= 2


@requires_pillow
def test_quadratic_marker_uses_real_tangent():
    # Quadratic starting with an upward tangent: the rendered tip points
    # north, hence the centroid offset points south.
    image = _render(
        '<path d="M100,160 Q100,40 160,40" fill="none" stroke="black" '
        'stroke-width="2" marker-start="url(#m)"/>')
    assert _angle_distance(_orientation(_red_pixels(image)), pi / 2) < 0.45


@requires_pillow
def test_arc_marker_follows_curvature():
    image = _render(
        '<path d="M40,100 A60,60 0 0 1 160,100" fill="none" stroke="black" '
        'stroke-width="2" marker-start="url(#m)" marker-end="url(#m)"/>')
    groups = _clusters(_red_pixels(image))
    assert len(groups) == 2
    angles = {}
    for group in groups:
        cx, _ = _centroid(group)
        angles['left' if cx < 100 else 'right'] = _orientation(group)
    # Left endpoint heads north (offset south), right one heads south.
    assert _angle_distance(angles['left'], pi / 2) < 0.45
    assert _angle_distance(angles['right'], -pi / 2) < 0.45
