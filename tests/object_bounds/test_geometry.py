"""Geometry regression tests for the object-bounding-box computation.

CairoSVG's :func:`cairosvg.bounding_box.bounding_box_path` is checked against
two independent implementations in :mod:`geometry`:

* an analytic box computed with the same math the spec describes but written
  from scratch (own tokenizer, own endpoint-to-center arc conversion),
* a dense flattening of the same path, which cannot share any assumption with
  the analytic code.

The boxes must agree with the analytic reference to floating-point precision
and with the flattened approximation within a tolerance that accounts for its
finite sampling resolution.
"""

import pytest

from cairosvg.parser import Tree
from cairosvg.bounding_box import (
    bounding_box_elliptical_arc, bounding_box_path,
    cubic_bezier_bounding_box, quadratic_bezier_bounding_box)

from geometry import (
    analytic_path_bbox, arc_endpoint_to_center, flattened_path_bbox)

ANALYTIC_TOLERANCE = 1e-9
FLATTEN_TOLERANCE = 1e-4

# Each item is a human-readable name and an SVG path data string.
PATH_CASES = [
    # --- line segments and moves -------------------------------------
    ('line-absolute', 'M10 20 L110 140'),
    ('line-relative', 'm10 20 l100 120'),
    ('horizontal-vertical', 'M5 5 H95 V75 h-30 v-40'),
    ('hv-relative', 'm5 5 h90 v70 h-30 v-40'),
    ('implicit-lineto', 'M10 10 90 10 90 90 10 90 Z'),
    ('negative-coordinates', 'M-60 -40 L-10 -5 L-30 -90 Z'),
    ('zero-length-moves', 'M10 10 M20 20 M30 30'),

    # --- cubic Beziers with extrema strictly inside the segment ------
    ('cubic-x-extremum', 'M0 0 C0 -100 100 -100 100 0'),
    ('cubic-y-extremum', 'M0 50 C20 -50 80 -50 100 50'),
    ('cubic-relative', 'm0 50 c20 -100 80 -100 100 0'),
    ('cubic-two-roots',
     'M0 0 C-40 60 140 60 100 0 C60 -60 40 -60 0 0'),
    ('cubic-negative', 'M-50 -50 C-120 -50 -120 50 -50 50'),
    ('cubic-degenerate-line', 'M0 0 L25 25 L75 75 L100 100'),

    # --- quadratic Beziers --------------------------------------------
    ('quadratic-peak', 'M0 0 Q50 -80 100 0'),
    ('quadratic-relative', 'm0 0 q50 80 100 0'),
    ('quadratic-no-inner-root', 'M0 0 Q10 10 20 30'),

    # --- smooth cubic S/s ---------------------------------------------
    ('smooth-cubic', 'M0 0 C10 10 20 10 30 0 S50 -10 60 0'),
    ('smooth-cubic-relative', 'm0 0 c10 10 20 10 30 0 s20 -10 30 0'),
    ('smooth-cubic-after-line', 'M0 0 L5 5 S40 -20 60 0'),
    ('smooth-cubic-chain', 'M0 0 C10 -20 20 -20 30 0 S50 20 60 0 S80 -20 90 0'),

    # --- smooth quadratic T/t -----------------------------------------
    ('smooth-quadratic', 'M0 0 Q15 -30 30 0 T60 0'),
    ('smooth-quadratic-relative', 'm0 0 q15 -30 30 0 t30 0'),
    ('smooth-quadratic-after-line', 'M0 0 L10 0 T40 20'),
    ('smooth-quadratic-chain', 'M0 0 Q10 -20 20 0 T40 0 T60 0'),

    # --- elliptical arcs: all flag combinations, rotation, radii ------
    ('arc-small-sweep0', 'M0 0 A40 20 30 0 0 60 40'),
    ('arc-small-sweep1', 'M0 0 A40 20 30 0 1 60 40'),
    ('arc-large-sweep0', 'M0 0 A40 20 30 1 0 60 40'),
    ('arc-large-sweep1', 'M0 0 A40 20 30 1 1 60 40'),
    ('arc-axis-aligned', 'M0 0 A50 30 0 1 1 100 0'),
    ('arc-quarter', 'M0 0 A40 40 0 0 1 40 40'),
    ('arc-relative', 'm10 10 a30 15 45 1 0 50 20'),
    ('arc-negative-coords', 'M-40 -20 A30 20 -20 1 1 20 -30'),
    ('arc-radii-too-small', 'M0 0 A5 3 0 1 1 60 0'),
    ('arc-zero-radius-line', 'M0 0 A0 0 0 1 1 40 30'),
    ('arc-same-endpoint', 'M10 10 A30 20 0 1 1 10 10'),

    # --- multiple subpaths and closed paths ---------------------------
    ('two-subpaths', 'M0 0 C0 -30 40 -30 40 0 M60 0 Q80 30 100 0'),
    ('closed-subpath', 'M10 10 C10 -20 90 -20 90 10 Z L50 60'),
    ('close-and-reopen', 'M0 0 Q25 -40 50 0 Z M60 10 L100 10 Z'),
    ('z-returns-to-subpath-start', 'M10 10 L40 10 L40 40 Z L10 40'),
]


def _cairosvg_box(path_data):
    node = Tree(bytestring=(
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<path d="' + path_data.encode() + b'"/></svg>'))
    return bounding_box_path(None, node.children[0])


def _to_minmax(box):
    minx, miny, width, height = box
    return minx, miny, minx + width, miny + height


@pytest.mark.parametrize('name, path_data', PATH_CASES)
def test_path_box_matches_analytic_reference(name, path_data):
    got = _to_minmax(_cairosvg_box(path_data))
    expected = analytic_path_bbox(path_data)
    assert expected is not None
    for actual_value, expected_value in zip(got, expected):
        assert abs(actual_value - expected_value) <= ANALYTIC_TOLERANCE, (
            f'{name}: {got} != {expected}')


@pytest.mark.parametrize('name, path_data', PATH_CASES)
def test_path_box_matches_flattened_reference(name, path_data):
    got = _to_minmax(_cairosvg_box(path_data))
    flattened = flattened_path_bbox(path_data, steps=4000)
    # Flattening slightly underestimates the extrema, so compare with a loose
    # tolerance; the strict check against the analytic code lives above.
    for actual_value, flattened_value in zip(got, flattened):
        assert abs(actual_value - flattened_value) <= FLATTEN_TOLERANCE, (
            f'{name}: {got} vs flattened {flattened}')


def test_empty_path_is_empty_box():
    node = Tree(bytestring=(
        b'<svg xmlns="http://www.w3.org/2000/svg"><path d=""/></svg>'))
    from cairosvg.bounding_box import (
        EMPTY_BOUNDING_BOX, is_valid_bounding_box)
    box = bounding_box_path(None, node.children[0])
    assert box == EMPTY_BOUNDING_BOX
    assert not is_valid_bounding_box(box)


def test_image_box_is_its_placement_rectangle():
    from cairosvg.bounding_box import bounding_box_image
    node = Tree(bytestring=(
        b'<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        b'<image x="12" y="34" width="56" height="78"/></svg>'))
    assert bounding_box_image(None, node.children[0]) == (12, 34, 56, 78)


def test_negative_coordinate_subpath_box():
    # The box must extend into negative coordinates rather than clamp at zero
    # or ignore a subpath that lies entirely outside the first quadrant.
    path_data = 'M-80 -60 C-120 -60 -120 0 -80 0 M20 20 L60 60'
    box = _to_minmax(_cairosvg_box(path_data))
    expected = analytic_path_bbox(path_data)
    assert box == pytest.approx(expected, abs=1e-9)
    assert box[0] < 0 and box[1] < 0  # genuinely reaches negative space


def test_close_path_returns_to_subpath_start():
    # After Z, a following line starts at the subpath origin; dropping that
    # state would misplace the closing segment's contribution.
    closed = _to_minmax(_cairosvg_box('M10 10 L40 10 L40 40 Z L10 40'))
    explicit = _to_minmax(
        _cairosvg_box('M10 10 L40 10 L40 40 L10 10 L10 40'))
    assert closed == explicit


def test_degenerate_path_has_zero_area_but_valid_box():
    # A straight horizontal line is valid geometry but has no height: it
    # cannot define an objectBoundingBox system.
    from cairosvg.bounding_box import is_non_empty_bounding_box
    box = _cairosvg_box('M0 50 L100 50')
    assert box == (0, 50, 100, 0)
    assert not is_non_empty_bounding_box(box)


def test_unrendered_text_has_no_geometry_box():
    # Text outlines depend on font metrics measured during rendering; before
    # that happens there is no analytical geometry box available.
    from cairosvg.bounding_box import bounding_box_text
    node = Tree(bytestring=(
        b'<svg xmlns="http://www.w3.org/2000/svg"><text>hello</text></svg>'))
    assert bounding_box_text(None, node.children[0]) is None


def test_zero_radius_arc_degenerates_to_endpoint_box():
    box = bounding_box_elliptical_arc(0, 0, 0, 0, 0, False, True, 40, 30)
    assert box == (0, 0, 40, 30)


def test_arc_with_identical_endpoints_is_a_point_box():
    box = bounding_box_elliptical_arc(
        10, 10, 30, 20, 0, True, True, 10, 10)
    assert box == (10, 10, 0, 0)


def test_bounding_box_does_not_mutate_node_or_state():
    node = Tree(bytestring=(
        b'<svg xmlns="http://www.w3.org/2000/svg">'
        b'<path d="M0 0 C0 -30 40 -30 40 0 S80 30 100 0"/></svg>'))
    path_node = node.children[0]
    original_d = path_node.get('d')
    box_1 = bounding_box_path(None, path_node)
    box_2 = bounding_box_path(None, path_node)
    assert box_1 == box_2
    assert path_node.get('d') == original_d
    assert not hasattr(path_node, 'vertices')


@pytest.mark.parametrize('points', [
    ((0, 0), (0, -100), (100, -100), (100, 0)),
    ((0, 50), (20, -50), (80, -50), (100, 50)),
])
def test_cubic_extrema_roots(points):
    box = cubic_bezier_bounding_box(*points)
    # The control polygon alone would give y in [-100, 0] for the first case;
    # the real curve never reaches y=-100 (its minimum is -75 at t=0.5).
    assert box[1] > min(point[1] for point in points) + 1e-9
    assert box[3] <= max(point[1] for point in points) + 1e-9


def test_quadratic_extrema_root():
    box = quadratic_bezier_bounding_box((0, 0), (50, -80), (100, 0))
    assert box == pytest.approx((0, -40, 100, 0), abs=1e-9)


@pytest.mark.parametrize('large, sweep', [
    (False, False), (False, True), (True, False), (True, True)])
def test_arc_box_against_center_parameterization(large, sweep):
    from math import cos, radians, sin
    x1, y1, rx, ry, phi, x2, y2 = 0, 0, 40, 20, radians(30), 60, 40
    params = arc_endpoint_to_center(
        x1, y1, rx, ry, phi, large, sweep, x2, y2)
    cx, cy, rx, ry, theta1, delta = params

    minx, miny, width, height = bounding_box_elliptical_arc(
        x1, y1, rx, ry, phi, large, sweep, x2, y2)
    maxx, maxy = minx + width, miny + height

    # Every sampled point of the center-parameterized arc must be inside the
    # reported box.
    cphi, sphi = cos(phi), sin(phi)
    for step in range(401):
        theta = theta1 + delta * step / 400
        px = cx + cphi * rx * cos(theta) - sphi * ry * sin(theta)
        py = cy + sphi * rx * cos(theta) + cphi * ry * sin(theta)
        assert minx - 1e-9 <= px <= maxx + 1e-9
        assert miny - 1e-9 <= py <= maxy + 1e-9

    # Both endpoints must be inside the reported box (they need not touch an
    # edge: for a large arc an interior extremum can dominate every axis).
    for px, py in ((x1, y1), (x2, y2)):
        assert minx - 1e-9 <= px <= maxx + 1e-9
        assert miny - 1e-9 <= py <= maxy + 1e-9

    # A tight box must be supported by an actual point of the arc on each
    # side: check that dense samples reach every boundary. Finite sampling
    # only approaches the true extremum, hence the 1e-5 tolerance (a control
    # point based box would be off by tens of units here).
    samples_x, samples_y = [], []
    for step in range(20001):
        theta = theta1 + delta * step / 20000
        samples_x.append(cphi * rx * cos(theta) - sphi * ry * sin(theta) + cx)
        samples_y.append(sphi * rx * cos(theta) + cphi * ry * sin(theta) + cy)
    assert abs(min(samples_x) - minx) <= 1e-5
    assert abs(max(samples_x) - maxx) <= 1e-5
    assert abs(min(samples_y) - miny) <= 1e-5
    assert abs(max(samples_y) - maxy) <= 1e-5
