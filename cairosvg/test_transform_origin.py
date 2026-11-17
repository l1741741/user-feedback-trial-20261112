"""
Tests for transform origin resolution, transform-box support and the
composition order of nested transformations.

The geometric tests render small SVG documents to in-memory PNG surfaces and
sample pixels at coordinates computed by hand, so that the visual result of
each transformation can be checked against the CSS Transforms specification
without depending on a full reference-renderer image baseline.

"""

import io
from math import cos, radians, sin

import cairocffi as cairo
import pytest

from . import svg2png
from .bounding_box import (
    calculate_bounding_box, is_non_empty_bounding_box)
from .helpers import (
    get_transform_reference_box, parse_transform_origin, transform)
from .parser import Tree
from .surface import PNGSurface


def filled(png_bytes, x, y):
    """Return whether pixel (x, y) paints an opaque, mostly-red color."""
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))
    buf = bytes(surface.get_data())
    width = surface.get_width()
    offset = (y * width + x) * 4
    blue, green, red, alpha = buf[offset:offset + 4]
    return alpha > 0 and red > green + 40 and red > blue + 40


def painted_bbox(png_bytes):
    """Return the (minx, miny, maxx, maxy) bbox of red pixels."""
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png_bytes))
    buf = bytes(surface.get_data())
    width, height = surface.get_width(), surface.get_height()
    points = []
    for y in range(height):
        for x in range(width):
            offset = (y * width + x) * 4
            blue, green, red, alpha = buf[offset:offset + 4]
            if alpha > 0 and red > green + 40 and red > blue + 40:
                points.append((x, y))
    assert points, 'expected at least one painted pixel'
    return (
        min(x for x, _ in points), min(y for _, y in points),
        max(x for x, _ in points), max(y for _, y in points))


def render(svg):
    if isinstance(svg, str):
        svg = svg.encode()
    return svg2png(svg)


def make_surface(width=200, height=200, context_width=None,
                 context_height=None, font_size=16):
    surface = PNGSurface.__new__(PNGSurface)
    surface.width = width
    surface.height = height
    surface.context_width = (
        context_width if context_width is not None else width)
    surface.context_height = (
        context_height if context_height is not None else height)
    surface.dpi = 96
    surface.font_size = font_size
    recording = cairo.RecordingSurface(cairo.CONTENT_COLOR, None)
    surface.context = cairo.Context(recording)
    return surface


def context_matrix(surface):
    return surface.context.get_matrix()


def rotated_point(point, angle, center):
    """Expected location of ``point`` rotated about ``center``."""
    x, y = point
    cx, cy = center
    rad = radians(angle)
    return (
        cx + (x - cx) * cos(rad) - (y - cy) * sin(rad),
        cy + (x - cx) * sin(rad) + (y - cy) * cos(rad))


# ---------------------------------------------------------------------------
# Pure resolution tests (no rendering)
# ---------------------------------------------------------------------------

def _tree(xml_body):
    return Tree(bytestring=(
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="200" height="100">{xml_body}</svg>'.encode()))


def test_parse_origin_keywords_and_lengths():
    surface = make_surface(200, 100)
    group = _tree('<g transform-origin="right bottom"/>').children[0]
    assert parse_transform_origin(surface, group) == (200, 100)

    group['transform-origin'] = 'left top'
    assert parse_transform_origin(surface, group) == (0, 0)

    group['transform-origin'] = 'top'
    assert parse_transform_origin(surface, group) == (100, 0)

    group['transform-origin'] = '25% 75%'
    assert parse_transform_origin(surface, group) == (50, 75)

    group['transform-origin'] = '30 40'
    assert parse_transform_origin(surface, group) == (30, 40)


def test_parse_origin_missing_returns_none():
    surface = make_surface()
    group = _tree('<g/>').children[0]
    assert parse_transform_origin(surface, group) is None


def test_parse_origin_illegal_structure_is_ignored():
    # Declarations with the wrong number of components are dropped entirely.
    surface = make_surface()
    group = _tree('<g/>').children[0]
    for bad_value in ('10 20 30', 'left right top', '10px 20px 30px'):
        group['transform-origin'] = bad_value
        assert parse_transform_origin(surface, group) is None


def test_parse_origin_unknown_keyword_falls_back_to_center():
    # A single unparsable component is treated per CSS as a bad coordinate,
    # which resolves to the center of the reference box rather than raising;
    # it must never produce a non-finite coordinate.
    surface = make_surface(200, 100)
    tree = Tree(bytestring=(
        b'<svg xmlns="http://www.w3.org/2000/svg" '
        b'width="200" height="100"><g/></svg>'))
    group = tree.children[0]
    group['transform-origin'] = 'foo bar'
    assert parse_transform_origin(surface, group) == (100, 50)

    group['transform-origin'] = '%%'
    assert parse_transform_origin(surface, group) == (100, 50)


def test_reference_box_defaults_to_view_box():
    surface = make_surface(200, 100)
    rect = _tree('<rect x="40" y="30" width="20" height="10"/>').children[0]
    assert get_transform_reference_box(surface, rect) == (0, 0, 200, 100)


def test_reference_box_fill_box_uses_geometry():
    surface = make_surface()
    rect = _tree(
        '<rect x="40" y="30" width="20" height="10" '
        'transform-box="fill-box"/>').children[0]
    assert get_transform_reference_box(surface, rect) == (40, 30, 20, 10)
    # Percentage origins then refer to the object box, not the viewport.
    rect['transform-origin'] = '50% 50%'
    assert parse_transform_origin(surface, rect) == (50, 35)


def test_reference_box_unknown_value_falls_back_to_view_box():
    surface = make_surface(200, 100)
    rect = _tree(
        '<rect x="40" y="30" width="20" height="10" '
        'transform-box="bogus-box"/>').children[0]
    assert get_transform_reference_box(surface, rect) == (0, 0, 200, 100)


def test_degenerate_bounding_box_falls_back_to_view_box():
    surface = make_surface(200, 120)
    rect = _tree(
        '<rect x="10" y="10" width="0" height="0" '
        'transform-box="fill-box"/>').children[0]
    box = calculate_bounding_box(surface, rect)
    assert not is_non_empty_bounding_box(box)
    assert get_transform_reference_box(surface, rect) == (0, 0, 200, 120)
    # The origin still resolves to a finite point on the viewport box.
    rect['transform-origin'] = 'center'
    assert parse_transform_origin(surface, rect) == (100, 60)


def test_group_fill_box_combines_children():
    surface = make_surface()
    group = _tree(
        '<g transform-box="fill-box">'
        '<rect x="10" y="20" width="30" height="40"/>'
        '<rect x="40" y="60" width="10" height="10"/></g>').children[0]
    assert get_transform_reference_box(surface, group) == (10, 20, 40, 50)


# ---------------------------------------------------------------------------
# Matrix composition tests
# ---------------------------------------------------------------------------

def test_transform_with_resolved_origin_matches_manual_matrix():
    surface = make_surface()
    transform(surface, 'rotate(90)', origin_xy=(50, 60))
    matrix = context_matrix(surface)
    # Forward map for a 90deg rotation about (50, 60):
    # x' = -y + 110, y' = x + 10.
    assert matrix.xx == pytest.approx(0, abs=1e-12)
    assert matrix.yx == pytest.approx(1, abs=1e-12)
    assert matrix.xy == pytest.approx(-1, abs=1e-12)
    assert matrix.yy == pytest.approx(0, abs=1e-12)
    assert matrix.x0 == pytest.approx(110, abs=1e-12)
    assert matrix.y0 == pytest.approx(10, abs=1e-12)


def test_origin_wraps_full_transform_list():
    # scale(2) then rotate(90), both about (10, 10): the origin translate
    # must wrap the whole operation list, not only the first operation.
    surface_a = make_surface()
    transform(surface_a, 'scale(2) rotate(90)', origin_xy=(10, 10))
    surface_b = make_surface()
    transform(
        surface_b,
        'translate(10 10) scale(2) rotate(90) translate(-10 -10)')
    a = context_matrix(surface_a)
    b = context_matrix(surface_b)
    for attr in ('xx', 'yx', 'xy', 'yy', 'x0', 'y0'):
        assert getattr(a, attr) == pytest.approx(
            getattr(b, attr), abs=1e-12)


def test_malformed_operation_keeps_valid_operations():
    surface = make_surface()
    transform(surface, 'translate(10, 0) bogus(??) translate(5, 0)')
    assert context_matrix(surface).x0 == pytest.approx(15, abs=1e-12)


def test_malformed_matrix_arity_is_skipped():
    surface = make_surface()
    transform(surface, 'matrix(1 2) translate(70, 0)')
    assert context_matrix(surface).x0 == pytest.approx(70, abs=1e-12)


# ---------------------------------------------------------------------------
# Rendered geometry tests
# ---------------------------------------------------------------------------

def _rect_svg(rect_attrs='', group_attrs='', size=120):
    return f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}"
         viewBox="0 0 {size} {size}">
      <g {group_attrs}>
        <rect x="20" y="20" width="40" height="20" fill="red" {rect_attrs}/>
      </g>
    </svg>'''


def test_rotation_about_explicit_length_origin_render():
    # Rotate the off-center rect 90deg about the explicit point (20, 20),
    # which is the rect's top-left corner: that corner stays fixed and the
    # rect, originally spanning x in [20, 59], now spans y in [20, 59].
    png = render(_rect_svg(
        group_attrs='transform="rotate(90)" transform-origin="20 20"'))
    assert painted_bbox(png) == (0, 20, 19, 59)
    assert filled(png, 19, 20)   # fixed corner neighborhood
    assert not filled(png, 40, 30)  # original interior is gone


def test_rotation_about_fill_box_center_render():
    # With fill-box, 50% 50% is the center of the rect itself (40, 30).
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"
         viewBox="0 0 120 120">
      <rect x="20" y="20" width="40" height="20" fill="red"
            transform-box="fill-box"
            transform-origin="50% 50%" transform="rotate(90)"/>
    </svg>'''
    png = render(svg)
    assert painted_bbox(png) == (30, 10, 49, 49)
    # The center point does not move.
    assert filled(png, 40, 30)
    # The pre-rotation top-right corner (60, 20) rotates 90deg about
    # (40, 30) to (50, 50); sample the interior pixel just beside it.
    expected = rotated_point((60, 20), 90, (40, 30))
    assert expected == pytest.approx((50, 50))
    assert filled(png, 49, 49)


def test_rotation_about_view_box_center_render():
    # Default view-box reference box: center of the 120x120 viewport.
    png = render(_rect_svg(
        group_attrs='transform="rotate(180)" transform-origin="center"'))
    # 180deg about (60, 60): rect (20,20,40,20) maps to (60,80,40,20).
    assert painted_bbox(png) == (60, 80, 99, 99)
    assert filled(png, 80, 90)


def test_percentage_origin_on_view_box_render():
    # 25% 75% of a 120x120 viewport is (30, 90). Rotating the rect 90deg
    # about that point yields a vertical rect in the bottom-right corner.
    png = render(_rect_svg(
        group_attrs='transform="rotate(90)" transform-origin="25% 75%"'))
    assert painted_bbox(png) == (80, 80, 99, 119)


def test_nested_groups_compose_in_list_order():
    # Inner group rotates 90deg about (30, 30); the outer group translates
    # by (50, 0). The inner rotation center moves to (80, 30) and remains
    # fixed relative to the rotated shape.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="160" height="120"
         viewBox="0 0 160 120">
      <g transform="translate(50 0)">
        <g transform="rotate(90)" transform-origin="30 30">
          <rect x="10" y="20" width="40" height="20" fill="red"/>
        </g>
      </g>
    </svg>'''
    png = render(svg)
    assert painted_bbox(png) == (70, 10, 89, 49)
    # The translated inner origin (80, 30) is painted.
    assert filled(png, 80, 30)
    # Local point (50, 20) rotates about (30, 30) to (40, 50), then the
    # outer translate moves it to (90, 50); sample one pixel inside the
    # antialiased boundary to avoid the edge pixel.
    assert filled(png, 85, 45)


def test_nested_origin_uses_local_coordinates():
    # The inner transform-origin tracks the rect's own fill box inside the
    # translated parent, instead of the whole canvas center.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="160" height="120"
         viewBox="0 0 160 120">
      <g transform="translate(40 10)">
        <rect x="0" y="0" width="20" height="20" fill="red"
              transform-box="fill-box"
              transform-origin="center" transform="rotate(180)"/>
      </g>
    </svg>'''
    png = render(svg)
    # 180deg about the local center (10, 10) leaves the rect in place,
    # translated to (40, 10); its global center is (50, 20).
    assert painted_bbox(png) == (40, 10, 59, 29)
    assert filled(png, 50, 20)


def test_illegal_origin_does_not_break_following_sibling():
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect x="10" y="10" width="20" height="20" fill="red"
            transform-origin="nonsense garbage" transform="rotate(90)"/>
      <rect x="80" y="80" width="20" height="20" fill="red"/>
    </svg>'''
    png = render(svg)
    # The malformed origin falls back to the center of the 120x120
    # reference box (60, 60): the rotated rect still paints, instead of
    # disappearing or raising, and the following sibling is untouched.
    assert filled(png, 90, 90)
    assert painted_bbox(png)[0] == 80  # sibling keeps its left edge
    assert any(filled(png, x, y)
               for x in range(80, 110) for y in range(10, 40))


def test_illegal_transform_does_not_break_node_or_sibling():
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect x="10" y="10" width="20" height="20" fill="red"
            transform="rotate(not-a-number)"/>
      <rect x="80" y="80" width="20" height="20" fill="red"/>
    </svg>'''
    png = render(svg)
    # The bad rotate is dropped: the first rect is painted untransformed
    # and the sibling is untouched.
    assert painted_bbox(png) == (10, 10, 99, 99)
    assert filled(png, 20, 20)
    assert filled(png, 90, 90)


def test_reused_use_node_is_position_independent():
    # The same referenced rect is instantiated twice at different
    # locations; each use must render at its own place and no state from
    # the first placement may leak into the second.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <defs>
        <rect id="r" width="20" height="20" fill="red"
              transform-box="fill-box"
              transform-origin="center" transform="rotate(90)"/>
      </defs>
      <use href="#r" x="10" y="40"/>
      <use href="#r" x="70" y="40"/>
    </svg>'''
    png = render(svg)
    # A 90deg rotation about the center leaves the square-shaped rect
    # covering the same 20x20 area at each translated position.
    assert painted_bbox(png) == (10, 40, 89, 59)
    assert filled(png, 20, 50)
    assert filled(png, 80, 50)


def test_repeated_renders_are_identical():
    svg = _rect_svg(
        rect_attrs='transform-box="fill-box" transform-origin="center" '
                   'transform="rotate(45)"')
    assert render(svg) == render(svg)


def test_degenerate_box_with_origin_still_renders_sibling():
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect x="0" y="0" width="0" height="0" fill="red"
            transform-box="fill-box" transform-origin="center"
            transform="rotate(90)"/>
      <rect x="50" y="50" width="20" height="20" fill="red"/>
    </svg>'''
    png = render(svg)
    assert painted_bbox(png) == (50, 50, 69, 69)


def test_default_behavior_without_origin_unchanged():
    # No transform-origin: a 90deg rotation stays about local origin (0,0),
    # which maps the whole rect to negative x, so nothing is painted.
    png = render(_rect_svg(group_attrs='transform="rotate(90)"'))
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    buf = bytes(surface.get_data())
    assert not any(buf[offset + 3] for offset in range(0, len(buf), 4))

    # Sanity check: the same rect without rotation paints normally.
    control = render(_rect_svg())
    assert painted_bbox(control) == (20, 20, 59, 39)


def test_origin_declared_via_css_style_attribute():
    # CSS declarations reach the node through the style attribute; the
    # fill-box origin must resolve exactly like the presentation form.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"
         viewBox="0 0 120 120">
      <rect x="20" y="20" width="40" height="20" fill="red"
            style="transform-box: fill-box; transform-origin: 50% 50%;
                   transform: rotate(90)"/>
    </svg>'''
    assert painted_bbox(render(svg)) == (30, 10, 49, 49)


def test_em_unit_origin_uses_current_font_size():
    # font-size is 20px, so transform-origin="1em 1em" is (20, 20);
    # a 180deg rotation about that point maps the rect to (0, 0, 40, 20).
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"
         viewBox="0 0 120 120">
      <g font-size="20">
        <rect x="20" y="20" width="40" height="20" fill="red"
              transform="rotate(180)" transform-origin="1em 1em"/>
      </g>
    </svg>'''
    assert painted_bbox(render(svg)) == (0, 0, 19, 19)


def test_fill_box_origin_inside_translated_parent():
    # The inner fill-box center is expressed in the parent's translated
    # coordinate system, so a 180deg rotation leaves the square in place.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="160" height="120"
         viewBox="0 0 160 120">
      <g transform="translate(40 10)">
        <g transform-box="fill-box" transform-origin="center"
           transform="rotate(180)">
          <rect x="0" y="0" width="20" height="20" fill="red"/>
        </g>
      </g>
    </svg>'''
    assert painted_bbox(render(svg)) == (40, 10, 59, 29)
