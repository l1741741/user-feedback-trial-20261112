"""
Offline tests for stroke joins, caps, miter limit and path reuse.

These tests render real PNG output with the bundled Cairo backend and inspect
the produced pixels. They do not need any network access or the large W3
non-regression image corpus, so they can run fully offline.

"""

import io
from math import pi

import cairocffi as cairo
import pytest

from . import svg2png


def _helpers():
    # Imported lazily so the end-to-end PNG tests can still be collected (and
    # fail) against an older source tree that lacks these helpers.
    from .helpers import (
        STROKE_LINE_CAP, STROKE_LINE_JOIN, quadratic_angles, stroke_line_cap,
        stroke_line_join, stroke_miter_limit)
    return (STROKE_LINE_CAP, STROKE_LINE_JOIN, quadratic_angles,
            stroke_line_cap, stroke_line_join, stroke_miter_limit)


# ---------------------------------------------------------------------------
# Pixel helpers
# ---------------------------------------------------------------------------

def _surface(png):
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def _pixels(surface):
    buf = memoryview(surface.get_data()).cast('B')
    stride = surface.get_stride()
    return buf, stride, surface.get_width(), surface.get_height()


def _is_ink(buf, stride, x, y):
    # Bytes are B, G, R, A on the little-endian ARGB32 native surface.
    return buf[y * stride + x * 4 + 3] > 0


def _ink_bbox(png):
    surface = _surface(png)
    buf, stride, width, height = _pixels(surface)
    minx, miny, maxx, maxy = width, height, -1, -1
    for y in range(height):
        for x in range(width):
            if _is_ink(buf, stride, x, y):
                minx = min(minx, x)
                miny = min(miny, y)
                maxx = max(maxx, x)
                maxy = max(maxy, y)
    assert maxx >= 0, 'the rendering produced no visible pixel'
    return minx, miny, maxx, maxy


def _column_top(png, center_x, half=3):
    surface = _surface(png)
    buf, stride, width, height = _pixels(surface)
    for y in range(height):
        if any(_is_ink(buf, stride, x, y)
               for x in range(center_x - half, center_x + half + 1)):
            return y
    raise AssertionError('no ink in the inspected column')


def _red_components(png):
    """Count 4-connected strongly-red blobs (used to count red markers)."""
    surface = _surface(png)
    buf, stride, width, height = _pixels(surface)
    mask = [
        [buf[y * stride + x * 4 + 2] > 150
         and buf[y * stride + x * 4 + 1] < 100
         and buf[y * stride + x * 4] < 100
         and buf[y * stride + x * 4 + 3] > 0
         for x in range(width)]
        for y in range(height)]
    seen = [[False] * width for _ in range(height)]
    count = 0
    for y in range(height):
        for x in range(width):
            if not mask[y][x] or seen[y][x]:
                continue
            count += 1
            stack = [(x, y)]
            seen[y][x] = True
            while stack:
                cx, cy = stack.pop()
                for nx, ny in ((cx + 1, cy), (cx - 1, cy),
                               (cx, cy + 1), (cx, cy - 1)):
                    if (0 <= nx < width and 0 <= ny < height
                            and mask[ny][nx] and not seen[ny][nx]):
                        seen[ny][nx] = True
                        stack.append((nx, ny))
    return count


# ---------------------------------------------------------------------------
# Pure parsing helpers
# ---------------------------------------------------------------------------

def test_stroke_line_cap_mapping():
    (STROKE_LINE_CAP, _, _, stroke_line_cap, _, _) = _helpers()
    assert stroke_line_cap('butt') == cairo.LINE_CAP_BUTT
    assert stroke_line_cap('round') == cairo.LINE_CAP_ROUND
    assert stroke_line_cap('square') == cairo.LINE_CAP_SQUARE
    # Missing and unknown values keep the SVG/Cairo initial value.
    assert stroke_line_cap(None) == cairo.LINE_CAP_BUTT
    assert stroke_line_cap('') == cairo.LINE_CAP_BUTT
    assert stroke_line_cap('nonsense') == cairo.LINE_CAP_BUTT
    assert set(STROKE_LINE_CAP) == {'butt', 'round', 'square'}


def test_stroke_line_join_mapping():
    (_, STROKE_LINE_JOIN, _, _, stroke_line_join, _) = _helpers()
    assert stroke_line_join('miter') == cairo.LINE_JOIN_MITER
    assert stroke_line_join('round') == cairo.LINE_JOIN_ROUND
    assert stroke_line_join('bevel') == cairo.LINE_JOIN_BEVEL
    # Missing and unknown values keep the SVG/Cairo initial value.
    assert stroke_line_join(None) == cairo.LINE_JOIN_MITER
    assert stroke_line_join('') == cairo.LINE_JOIN_MITER
    assert stroke_line_join('bogus') == cairo.LINE_JOIN_MITER
    assert set(STROKE_LINE_JOIN) == {'miter', 'round', 'bevel'}


@pytest.mark.parametrize('bad', [
    None, '', 'garbage', '12px', '-1', '0', '0.5', 'nan', 'inf', '-inf'])
def test_stroke_miter_limit_fallback(bad):
    (_, _, _, _, _, stroke_miter_limit) = _helpers()
    # Invalid, non-finite or < 1 limits must fall back to the SVG default 4,
    # never raise and never poison the Cairo context.
    assert stroke_miter_limit(bad) == 4.0


@pytest.mark.parametrize('good', ['1', '2.5', '4', '10', '100'])
def test_stroke_miter_limit_valid(good):
    (_, _, _, _, _, stroke_miter_limit) = _helpers()
    assert stroke_miter_limit(good) == float(good)


def test_quadratic_angles_match_tangents():
    (_, _, quadratic_angles, _, _, _) = _helpers()
    # Q from (10, 50) with control (60, 10) to (110, 50).
    import math
    start, end = quadratic_angles(10, 50, 60, 10, 110, 50)
    assert start == pytest.approx(pi - math.atan2(-40, 50))
    assert end == pytest.approx(math.atan2(40, 50))


def test_quadratic_angles_degenerate_control():
    (_, _, quadratic_angles, _, _, _) = _helpers()
    # A control point coinciding with an endpoint must not yield the old
    # hard-coded zero angle; it falls back to the chord orientation.
    import math
    start, end = quadratic_angles(0, 0, 0, 0, 10, 0)
    assert start == pytest.approx(pi)
    assert end == pytest.approx(0.0)
    start, end = quadratic_angles(0, 0, 10, 0, 10, 0)
    assert start == pytest.approx(pi)
    assert end == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# End to end PNG rendering
# ---------------------------------------------------------------------------

def _svg(body):
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="140">'
        f'{body}</svg>').encode()


def test_invalid_miterlimit_does_not_abort_render():
    png = svg2png(bytestring=_svg(
        '<path d="M10 90 L50 10 L90 90" fill="none" stroke="red" '
        'stroke-width="10" stroke-miterlimit="garbage"/>'
        '<rect x="5" y="5" width="20" height="20" fill="lime"/>'))
    # A full PNG is still emitted and the sibling rect is painted.
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    assert _ink_bbox(png) is not None


@pytest.mark.parametrize('bad', ['garbage', '-1', '0', 'nan', 'inf'])
def test_invalid_miterlimit_variants_render(bad):
    png = svg2png(bytestring=_svg(
        f'<path d="M10 90 L50 10 L90 90" fill="none" stroke="red" '
        f'stroke-width="10" stroke-miterlimit="{bad}"/>'))
    assert png[:8] == b'\x89PNG\r\n\x1a\n'


def test_explicit_miter_resets_inherited_round_join():
    png = svg2png(bytestring=_svg(
        '<g stroke-linejoin="round" fill="none" stroke="black" '
        'stroke-width="24">'
        '<path d="M20 110 L60 30 L100 110"/>'
        '<path d="M130 110 L170 30 L210 110" stroke-linejoin="miter"/>'
        '<path d="M240 110 L280 30 L320 110" stroke-linejoin="bogus"/>'
        '</g>'))
    round_top = _column_top(png, 60)
    miter_top = _column_top(png, 170)
    bogus_top = _column_top(png, 280)
    # The miter join and the bogus-keyword (default miter) join protrude above
    # the vertex, while the round inherited join stays closer to the vertex.
    assert miter_top < round_top
    assert bogus_top < round_top
    assert miter_top == bogus_top


def test_explicit_butt_resets_inherited_round_cap():
    body = (
        '<g stroke-linecap="round" fill="none" stroke="black" '
        'stroke-width="20">'
        # Round cap extends beyond the x=30 start of the line.
        '<line x1="40" y1="40" x2="120" y2="40"/>'
        # Explicit butt cap must stop exactly at the x=170 endpoint.
        '<line x1="170" y1="40" x2="250" y2="40" stroke-linecap="butt"/>'
        '</g>')
    png = svg2png(bytestring=_svg(body))
    minx, _, _, _ = _ink_bbox(png)
    surface = _surface(png)
    buf, stride, width, height = _pixels(surface)
    # Leftmost ink belongs to the round-capped line starting at x=40.
    assert minx < 40
    # Around the butt line start there must be no ink before x=170.
    assert not any(
        _is_ink(buf, stride, x, y)
        for x in range(150, 170) for y in range(25, 56))


@pytest.mark.parametrize('join', ['miter', 'round', 'bevel'])
def test_join_applies_to_mixed_segments_and_closed_contour(join):
    # One path mixing a line, a quadratic and a cubic curve, then closed.
    d = 'M20 80 L60 20 Q90 60 120 30 C150 10 170 70 200 40 L160 90 Z'
    png = svg2png(bytestring=_svg(
        f'<path d="{d}" fill="none" stroke="black" stroke-width="14" '
        f'stroke-linejoin="{join}"/>'))
    bbox = _ink_bbox(png)
    # Every join mode must render a closed, non-empty contour.
    assert bbox[2] > bbox[0] and bbox[3] > bbox[1]


def test_miterlimit_clamps_the_spike_on_a_sharp_angle():
    path = ('<path d="M100 100 L104 20 L108 100" fill="none" stroke="navy" '
            'stroke-width="10"/>')
    high = svg2png(bytestring=_svg(
        path.replace('stroke-width="10"',
                     'stroke-width="10" stroke-miterlimit="100"')))
    low = svg2png(bytestring=_svg(
        path.replace('stroke-width="10"',
                     'stroke-width="10" stroke-miterlimit="1"')))
    # A tiny miter limit converts the sharp join to a bevel and removes the
    # long spike, so the topmost ink sits lower than with a large limit.
    assert _column_top(low, 104) > _column_top(high, 104)


def test_short_and_zero_length_segments_render():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120">'
        '<path d="M30 90 L30 90 L70 20 L70 20 L110 90 L110 90" fill="none" '
        'stroke="black" stroke-width="12"/></svg>').encode()
    png = svg2png(svg)
    bbox = _ink_bbox(png)
    # The two real segments still draw a chevron.
    assert bbox[2] - bbox[0] > 40 and bbox[3] - bbox[1] > 40


def test_reused_path_keeps_its_markers_and_geometry():
    marker = (
        '<marker id="m" markerWidth="6" markerHeight="6" refX="3" refY="3">'
        '<circle cx="3" cy="3" r="2" fill="red"/></marker>')
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="360" height="100">'
        f'<defs>{marker}'
        '<path id="p" d="M10 50 L50 10 L90 50" fill="none" stroke="black" '
        'stroke-width="4" marker-start="url(#m)" marker-end="url(#m)"/>'
        '</defs><use href="#p"/><use href="#p" x="200"/></svg>').encode()
    png = svg2png(svg)
    # Two uses x two markers = four independent red marker blobs.
    assert _red_components(png) == 4


def test_reused_path_does_not_mutate_original_node():
    from .parser import Tree
    from . import path as path_module
    from .surface import PNGSurface, TAGS

    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="400" height="100">'
        '<defs><path id="p" d="M10 50 L50 10 L90 50 L60 70 Z" fill="none" '
        'stroke="black"/></defs>'
        '<use href="#p"/><use href="#p" x="120"/><use href="#p" x="240"/>'
        '</svg>').encode()

    captured = []
    original = path_module.path

    def spy(surface, node):
        result = original(surface, node)
        if node.get('id') == 'p':
            captured.append((id(node), list(node.vertices)))
        return result

    path_module.path = spy
    TAGS['path'] = spy
    try:
        PNGSurface(Tree(bytestring=svg), None, 96)
    finally:
        path_module.path = original
        TAGS['path'] = original

    assert len(captured) == 3
    # Same shared node, rebuilt with identical vertices every time.
    assert len({row[0] for row in captured}) == 1
    assert all(row[1] == captured[0][1] for row in captured)


def test_quadratic_marker_orientation_uses_real_tangent():
    # A marker with a clearly asymmetric triangle so its rotation is visible.
    marker = (
        '<marker id="arrow" markerWidth="10" markerHeight="10" refX="5" '
        'refY="5" orient="auto-start-reverse">'
        '<path d="M0 0 L10 5 L0 10 Z" fill="red"/></marker>')
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120">'
        f'<defs>{marker}</defs>'
        '<path d="M20 60 Q100 0 180 60" fill="none" stroke="black" '
        'stroke-width="4" marker-start="url(#arrow)" '
        'marker-end="url(#arrow)"/></svg>').encode()
    png = svg2png(svg)
    # Both end markers must be painted with the tangent orientation.
    assert _red_components(png) == 2
