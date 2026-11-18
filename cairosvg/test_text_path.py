"""
Test suite for text laid out along a path (``textPath``).

These tests exercise the layout rules of SVG 1.1 section 10.13:

* ``startOffset`` percentages are relative to the referenced path length;
* ``text-anchor`` and per-character ``dx``/``dy`` follow the path direction;
* multiple subpaths, closed curves and zero-length paths have deterministic
  results;
* a path too short never leaks layout state into following ordinary text;
* referencing the same path several times does not mutate its geometry.

Glyph positions are read back from a recording surface by intercepting the
cairo ``text_path`` call, so the tests check actual rendered glyph placement
and order rather than string output. Font rasterization is not pixel exact,
so coordinates are compared with a small tolerance.

"""

import cairocffi as cairo
import pytest

from .parser import Tree
from .surface import Surface
from .text import (
    parse_start_offset, path_length, point_following_path, subpath_segments)

FONT = 'Verdana'
# The recording surface is point-based while the SVG viewport is pixel-based,
# so cairo applies a points-per-pixel scale. Coordinates read back from the
# glyph matrix are divided by this factor to recover SVG user units.
POINT_SCALE = 72 / 96
# Tolerance in user units: font hinting and curve flattening introduce small
# variations, this is comfortably above rasterization noise but well below a
# glyph advance (about 10-14 units at the font sizes used below).
TOL = 1.5


class RecordingSurface(Surface):
    """Surface recording vector operations instead of rasterizing them."""

    surface_class = cairo.RecordingSurface

    def _create_surface(self, width, height):
        return (
            cairo.RecordingSurface(cairo.CONTENT_COLOR_ALPHA, None),
            width, height)


def _user_units(value):
    """Convert a device-space length back to SVG user units."""
    return value / POINT_SCALE


def _path_points(path):
    for item in path:
        if item[0] in (cairo.PATH_MOVE_TO, cairo.PATH_LINE_TO):
            yield item[1]
        elif item[0] == cairo.PATH_CURVE_TO:
            values = item[1]
            for index in (0, 2, 4):
                yield values[index], values[index + 1]


def render_glyphs(svg):
    """Render ``svg`` and return one record per glyph of the fill pass.

    Each record holds the character, its baseline origin in SVG user units
    and the 2x2 part of the cairo matrix (useful to detect reversed glyphs).

    """
    records = []
    original_text_path = cairo.Context.text_path
    original_show_text = cairo.Context.show_text

    def probe_text_path(context, text):
        matrix = context.get_matrix()
        original_text_path(context, text)
        glyph_path = context.copy_path()
        points = list(_path_points(glyph_path))
        bbox = None
        if points:
            xs = [point[0] for point in points]
            ys = [point[1] for point in points]
            bbox = (
                _user_units(min(xs)), _user_units(min(ys)),
                _user_units(max(xs)), _user_units(max(ys)))
        # text_extents reports font metrics in user units, not device space.
        advance = context.text_extents(text)[4]
        records.append({
            'char': text,
            'x': _user_units(matrix.x0), 'y': _user_units(matrix.y0),
            'xx': matrix.xx / POINT_SCALE, 'yx': matrix.yx / POINT_SCALE,
            'xy': matrix.xy / POINT_SCALE, 'yy': matrix.yy / POINT_SCALE,
            'advance': advance, 'bbox': bbox})

    # The fill pass uses show_text: record it but keep only the first,
    # text_path-based pass to avoid duplicate entries.
    def probe_show_text(context, text):
        matrix = context.get_matrix()
        records.append({
            'char': text,
            'x': _user_units(matrix.x0), 'y': _user_units(matrix.y0),
            'xx': matrix.xx / POINT_SCALE,
            'yx': matrix.yx / POINT_SCALE,
            'xy': matrix.xy / POINT_SCALE,
            'yy': matrix.yy / POINT_SCALE, 'bbox': None})

    cairo.Context.text_path = probe_text_path
    cairo.Context.show_text = probe_show_text
    try:
        surface = RecordingSurface(Tree(bytestring=svg.encode()), None, 96)
        surface.finish()
    finally:
        cairo.Context.text_path = original_text_path
        cairo.Context.show_text = original_show_text

    # Every glyph is processed twice (text_path then show_text); keep the
    # text_path records, which carry a bounding box.
    return [record for record in records if record['bbox'] is not None]


def render_png(svg):
    """Render ``svg`` to a PNG byte string."""
    from . import svg2png
    return svg2png(bytestring=svg.encode())


def svg(body, width=300, height=200):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}">{body}</svg>')


def text_path_svg(path_d, text, text_attrs='', path_attrs=''):
    return svg(
        f'<path id="p" d="{path_d}" {path_attrs}/>'
        f'<text font-family="{FONT}" font-size="20" {text_attrs}>'
        f'<textPath href="#p">{text}</textPath></text>')


def test_basic_horizontal_layout_order():
    glyphs = render_glyphs(text_path_svg('M20 60 H220', 'ABC'))
    assert [glyph['char'] for glyph in glyphs] == ['A', 'B', 'C']
    # Glyphs advance left to right, horizontally.
    xs = [glyph['x'] for glyph in glyphs]
    assert xs[0] < xs[1] < xs[2]
    for glyph in glyphs:
        assert glyph['yx'] == pytest.approx(0, abs=1e-6)
        assert glyph['xx'] > 0


def test_start_offset_numeric():
    baseline = render_glyphs(text_path_svg('M20 60 H220', 'A'))
    shifted = render_glyphs(svg(
        '<path id="p" d="M20 60 H220"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p" startOffset="40">A</textPath></text>'))
    # A 40 user-unit offset moves the first glyph by 40 along the path.
    assert shifted[0]['x'] - baseline[0]['x'] == pytest.approx(40, abs=TOL)


def test_start_offset_percentage_uses_path_length():
    # The path is 200 units long: 50% must place the first glyph at 100.
    glyphs = render_glyphs(svg(
        '<path id="p" d="M20 60 H220"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p" startOffset="50%">A</textPath></text>'))
    assert glyphs[0]['x'] == pytest.approx(120, abs=TOL)


def test_start_offset_clamped_to_path():
    # An offset beyond the path end is clamped: nothing is drawn past it.
    glyphs = render_glyphs(svg(
        '<path id="p" d="M20 60 H120"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p" startOffset="150">AB</textPath></text>'))
    assert glyphs == []


def glyph_point(glyph, local_x, local_y):
    """Map a glyph-local point to global coordinates through its matrix."""
    return (
        glyph['x'] + glyph['xx'] * local_x + glyph['xy'] * local_y,
        glyph['y'] + glyph['yx'] * local_x + glyph['yy'] * local_y)


def glyph_extent(glyph, edge):
    """Global x coordinate of a glyph bounding-box ``edge`` (0 left, 2 right).

    The bounding box is expressed in the glyph's local (rotated, translated)
    coordinate system; the full cairo matrix maps it onto global space.

    """
    # Use the mid-height point so a rotated glyph still projects correctly.
    mid_y = (glyph['bbox'][1] + glyph['bbox'][3]) / 2
    return glyph_point(glyph, glyph['bbox'][edge], mid_y)[0]


def test_text_anchor_middle_with_percentage():
    # 50% + middle: the center of the run's total advance sits on the path
    # midpoint x = 120.
    glyphs = render_glyphs(svg(
        '<path id="p" d="M20 60 H220"/>'
        f'<text font-family="{FONT}" font-size="20" text-anchor="middle">'
        '<textPath href="#p" startOffset="50%">ABC</textPath></text>'))
    run_start = glyphs[0]['x']
    run_end = glyphs[-1]['x'] + glyphs[-1]['advance']
    assert (run_start + run_end) / 2 == pytest.approx(120, abs=TOL)


def test_text_anchor_end_at_path_end():
    # 100% + end: the run's total advance ends exactly at the path end 220.
    glyphs = render_glyphs(svg(
        '<path id="p" d="M20 60 H220"/>'
        f'<text font-family="{FONT}" font-size="20" text-anchor="end">'
        '<textPath href="#p" startOffset="100%">AB</textPath></text>'))
    run_end = glyphs[-1]['x'] + glyphs[-1]['advance']
    assert run_end == pytest.approx(220, abs=TOL)


def test_reversed_path_glyphs_follow_direction():
    glyphs = render_glyphs(text_path_svg('M220 60 H20', 'ABC'))
    xs = [glyph['x'] for glyph in glyphs]
    # Glyphs advance right to left following the reversed path.
    assert xs[0] > xs[1] > xs[2]
    for glyph in glyphs:
        assert glyph['xx'] < 0  # glyph rotated ~180 degrees


def test_dx_advances_along_path_direction():
    plain = render_glyphs(text_path_svg('M20 60 H220', 'A'))
    shifted = render_glyphs(svg(
        '<path id="p" d="M20 60 H220"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p" dx="10">A</textPath></text>'))
    assert shifted[0]['x'] - plain[0]['x'] == pytest.approx(10, abs=TOL)

    # On a reversed path, a positive dx moves towards decreasing x.
    reversed_plain = render_glyphs(text_path_svg('M220 60 H20', 'A'))
    reversed_shifted = render_glyphs(svg(
        '<path id="p" d="M220 60 H20"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p" dx="10">A</textPath></text>'))
    assert (reversed_shifted[0]['x'] - reversed_plain[0]['x']) == pytest.approx(
        -10, abs=TOL)


def test_dy_shifts_perpendicularly():
    plain = render_glyphs(text_path_svg('M20 60 H220', 'A'))
    shifted = render_glyphs(svg(
        '<path id="p" d="M20 60 H220"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p" dy="6">A</textPath></text>'))
    assert shifted[0]['y'] - plain[0]['y'] == pytest.approx(6, abs=TOL)


def test_multiple_subpaths_do_not_jump_the_gap():
    # Two 40-unit subpaths separated by a 60-unit gap.
    glyphs = render_glyphs(text_path_svg(
        'M20 60 H60 M120 60 H220', 'ABCDEFGHIJ'))
    # The 4th glyph starts the second subpath: it must sit at x = 120,
    # never at an interpolated position inside the gap.
    fourth = glyphs[3]
    assert fourth['x'] == pytest.approx(120, abs=TOL)
    # No glyph is ever placed inside the gap (60 < x < 120).
    for glyph in glyphs:
        assert not (60 + TOL < glyph['x'] < 120 - TOL)


def test_closed_curve_includes_closing_segment():
    # Triangle: two sides of 100 and 60 units and a closing diagonal. Without
    # the closing segment the total length would miss the third side.
    from .text import build_text_path
    surface = RecordingSurface(
        Tree(bytestring=svg(
            '<path id="p" d="M20 100 L120 100 L120 40 Z"/>').encode()),
        None, 96)
    node = surface.paths['p']
    flat = build_text_path(surface, node)
    expected = 100 + 60 + (100 ** 2 + 60 ** 2) ** .5
    assert path_length(flat) == pytest.approx(expected, abs=TOL)
    assert len(subpath_segments(flat)) == 1
    surface.finish()


def test_zero_length_path_disables_content():
    glyphs = render_glyphs(svg(
        '<path id="p" d="M20 60 L20 60"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p">ABC</textPath></text>'))
    assert glyphs == []


def test_missing_href_disables_content():
    glyphs = render_glyphs(svg(
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#nope">ABC</textPath></text>'))
    assert glyphs == []


def test_short_path_does_not_leak_into_sibling_text():
    glyphs = render_glyphs(svg(
        '<path id="p" d="M20 60 H40"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p">ABCDEFGH</textPath>'
        '<tspan x="20" y="120">XYZ</tspan></text>'))
    # Baseline: the same ordinary tspan with no preceding textPath.
    baseline = render_glyphs(svg(
        f'<text font-family="{FONT}" font-size="20">'
        '<tspan x="20" y="120">XYZ</tspan></text>'))
    sibling = [glyph for glyph in glyphs if glyph['char'] in 'XYZ']
    base = [glyph for glyph in baseline if glyph['char'] in 'XYZ']
    # The sibling tspan is positioned exactly like the standalone one: no
    # offset inherited from the exhausted path.
    assert len(sibling) == 3
    for got, expected in zip(sibling, base):
        assert got['bbox'] == pytest.approx(expected['bbox'], abs=TOL)


def test_sibling_plain_text_after_zero_length_path():
    glyphs = render_glyphs(svg(
        '<path id="p" d="M20 60 L20 60"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p">ABC</textPath></text>'
        f'<text x="20" y="120" font-family="{FONT}" font-size="20">'
        'XYZ</text>'))
    baseline = render_glyphs(svg(
        f'<text x="20" y="120" font-family="{FONT}" font-size="20">'
        'XYZ</text>'))
    chars = [glyph['char'] for glyph in glyphs]
    # The disabled textPath contributes no glyphs at all.
    assert 'A' not in chars and 'B' not in chars and 'C' not in chars
    assert chars == [glyph['char'] for glyph in baseline]
    # The following plain text is placed exactly where it would be alone.
    for got, expected in zip(glyphs, baseline):
        assert got['bbox'] == pytest.approx(expected['bbox'], abs=TOL)


def test_nested_tspan_continues_path_layout():
    glyphs = render_glyphs(svg(
        '<path id="p" d="M20 60 H220"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p">AB<tspan>CD</tspan></textPath></text>'))
    assert [glyph['char'] for glyph in glyphs] == ['A', 'B', 'C', 'D']
    xs = [glyph['x'] for glyph in glyphs]
    assert xs == sorted(xs)
    # No reset: the third glyph continues right after the second.
    assert xs[2] > xs[1]


def test_polyline_shape_as_text_path():
    glyphs = render_glyphs(svg(
        '<polyline id="pl" points="20,100 80,40 140,100" fill="none"/>'
        f'<text font-family="{FONT}" font-size="16">'
        '<textPath href="#pl">POLY</textPath></text>'))
    assert [glyph['char'] for glyph in glyphs] == list('POLY')
    # Glyphs climb the first slope: y decreases as x increases.
    assert glyphs[0]['y'] > glyphs[1]['y']
    assert glyphs[1]['x'] > glyphs[0]['x']


def test_circle_shape_as_text_path():
    glyphs = render_glyphs(svg(
        '<circle id="c" cx="150" cy="150" r="100" fill="none"/>'
        f'<text font-family="{FONT}" font-size="16">'
        '<textPath href="#c">CIRCLE</textPath></text>',
        width=300, height=300))
    assert [glyph['char'] for glyph in glyphs] == list('CIRCLE')
    # Glyph orientations vary as they follow the circle.
    angles = {glyph['char']: glyph['yx'] for glyph in glyphs}
    assert len({round(value, 3) for value in angles.values()}) > 1


def test_repeated_reference_keeps_geometry():
    body = (
        '<path id="p" d="M20 60 H220" fill="none" stroke="red"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p">ABC</textPath></text>'
        f'<text font-family="{FONT}" font-size="20" transform="translate(0,50)">'
        '<textPath href="#p">DEF</textPath></text>')
    first = render_glyphs(svg(body))
    second = render_glyphs(svg(body))
    assert [(g['char'], round(g['x'], 4), round(g['y'], 4)) for g in first] == \
        [(g['char'], round(g['x'], 4), round(g['y'], 4)) for g in second]


def test_referenced_path_still_draws_correctly():
    # The visible path and the textPath share one node: the stroke must keep
    # its original geometry.
    png = render_png(svg(
        '<path id="p" d="M20 60 H220" stroke="black" stroke-width="2"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p">ABC</textPath></text>'))
    assert png[:8] == b'\x89PNG\r\n\x1a\n'
    # A second render must be byte-identical: geometry is not mutated.
    assert render_png(svg(
        '<path id="p" d="M20 60 H220" stroke="black" stroke-width="2"/>'
        f'<text font-family="{FONT}" font-size="20">'
        '<textPath href="#p">ABC</textPath></text>')) == png


def test_path_transform_applied_to_text():
    plain = render_glyphs(text_path_svg('M20 60 H220', 'ABC'))
    transformed = render_glyphs(svg(
        '<path id="p" d="M20 60 H220"/>'
        f'<text font-family="{FONT}" font-size="20" '
        'transform="translate(0,40)">'
        '<textPath href="#p">ABC</textPath></text>'))
    for a, b in zip(plain, transformed):
        assert b['y'] - a['y'] == pytest.approx(40, abs=TOL)
        assert b['x'] == pytest.approx(a['x'], abs=TOL)


def test_path_length_and_point_helpers():
    surface = RecordingSurface(
        Tree(bytestring=svg(
            '<path id="p" d="M10 10 H110 M200 200 V300 Z"/>').encode()),
        None, 96)
    node = surface.paths['p']
    from .text import build_text_path
    flat = build_text_path(surface, node)
    # First subpath 100; second subpath goes down 100 then closes back up 100.
    assert path_length(flat) == pytest.approx(300, abs=TOL)
    point = point_following_path(flat, 50)
    assert point[0] == pytest.approx(60, abs=TOL)
    assert point[1] == pytest.approx(10, abs=TOL)
    assert point[2] == pytest.approx(0, abs=1e-6)
    # Position beyond the path yields None rather than a stale last point.
    assert point_following_path(flat, 500) is None
    # Negative distances are rejected rather than placed before the start.
    assert point_following_path(flat, -1) is None
    surface.finish()


def test_consecutive_move_to_subpaths_are_distinct():
    context = cairo.Context(
        cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10))
    context.move_to(0, 0)
    context.line_to(10, 0)
    context.move_to(50, 50)          # lone move-to subpath
    context.move_to(100, 0)
    context.line_to(130, 0)
    flat = context.copy_path_flat()
    subpaths = subpath_segments(flat)
    # The two drawn segments survive; the lone intermediate move-to point is
    # not turned into a length-bearing subpath.
    assert len(subpaths) == 2
    assert path_length(flat) == pytest.approx(40, abs=TOL)
    # The end of the first subpath is reported at its own end point.
    assert point_following_path(flat, 10)[0] == pytest.approx(10, abs=TOL)
    # Walking past it lands at the second subpath instead of inside the gap.
    assert point_following_path(flat, 10 + 1e-6)[0] == pytest.approx(
        100, abs=TOL)


def test_subpath_segments_close_handling():
    context = cairo.Context(
        cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10))
    context.move_to(0, 0)
    context.line_to(10, 0)
    context.close_path()
    subpaths = subpath_segments(context.copy_path_flat())
    assert len(subpaths) == 1
    # Closing segment back to the origin is materialized.
    assert subpaths[0][-1] == (0, 0)


def test_parse_start_offset_percentage_and_clamp():
    surface = RecordingSurface(
        Tree(bytestring=svg('<g/>').encode()), None, 96)
    assert parse_start_offset(surface, '50%', 200) == 100
    assert parse_start_offset(surface, '40', 200) == 40
    assert parse_start_offset(surface, '250', 200) == 200
    assert parse_start_offset(surface, '-30', 200) == 0
    assert parse_start_offset(surface, 'garbage', 200) == 0
    surface.finish()
