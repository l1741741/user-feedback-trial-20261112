"""
Tests for text laid out on a path (``textPath``).

The rendered glyph positions are captured by recording the current
transformation matrix of the cairo context whenever CairoSVG draws a
character. This checks the *layout* (position, order and direction of the
glyphs) directly, instead of comparing rasterized pixels: font hinting and
anti-aliasing can move ink by a fraction of a pixel, while the layout
itself is deterministic. A tolerance of one user unit is enough to absorb
font rasterization differences.

"""

import math

import cairocffi as cairo
import pytest

from . import svg2png

POSITION_TOLERANCE = 1.0
ANGLE_TOLERANCE = 3.0

GLYPHS = []

_orig_text_path = cairo.Context.text_path
_orig_show_text = cairo.Context.show_text


def _record_glyph(context, letter):
    if not letter or letter.isspace():
        return
    matrix = context.get_matrix()
    point_x, point_y = context.get_current_point()
    x = matrix.xx * point_x + matrix.xy * point_y + matrix.x0
    y = matrix.yx * point_x + matrix.yy * point_y + matrix.y0
    angle = math.degrees(math.atan2(matrix.yx, matrix.xx))
    GLYPHS.append((letter, round(x, 3), round(y, 3), round(angle, 3)))


def _text_path(self, string):
    # Only the outline pass is recorded: the fill pass (show_text) draws
    # the same glyphs with the exact same layout a second time.
    _record_glyph(self, string)
    return _orig_text_path(self, string)


def _show_text(self, string):
    return _orig_show_text(self, string)


cairo.Context.text_path = _text_path
cairo.Context.show_text = _show_text


def render(svg_body):
    """Render an SVG snippet and return the recorded glyphs."""
    GLYPHS.clear()
    svg = f'''
    <svg xmlns="http://www.w3.org/2000/svg"
         xmlns:xlink="http://www.w3.org/1999/xlink"
         width="220" height="160">
      {svg_body}
    </svg>'''
    svg2png(bytestring=svg.encode('utf-8'))
    return list(GLYPHS)


def text_width(text, font_size=10):
    """Return ``(x_bearing, width)`` of text rendered with Arial."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10)
    context = cairo.Context(surface)
    context.select_font_face('Arial')
    context.set_font_size(font_size)
    x_bearing, _, width, _, _, _ = context.text_extents(text)
    return x_bearing, width


def assert_point(glyph, x=None, y=None, angle=None):
    """Check one recorded glyph against the expected location."""
    letter, glyph_x, glyph_y, glyph_angle = glyph
    if x is not None:
        assert abs(glyph_x - x) <= POSITION_TOLERANCE, glyph
    if y is not None:
        assert abs(glyph_y - y) <= POSITION_TOLERANCE, glyph
    if angle is not None:
        delta = abs((glyph_angle - angle + 180) % 360 - 180)
        assert delta <= ANGLE_TOLERANCE, glyph


def assert_order(glyphs, letters):
    """Recorded glyphs must appear in this exact order."""
    assert [glyph[0] for glyph in glyphs] == list(letters)


def test_start_offset_length_percentage():
    """A percentage startOffset is relative to the path length."""
    glyphs = render('''
      <path id="p" d="M10 50 L90 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p" startOffset="25%">AB</textPath>
      </text>''')
    assert len(glyphs) == 2
    # 25% of the 80-unit long path is 20 units: the glyph starts at x=30.
    # Using a percentage other than 50% and a non-viewport-sized path makes
    # this fail if percentages are resolved against the viewport diagonal.
    assert_point(glyphs[0], x=30, y=50, angle=0)
    # The second glyph follows the first one along the path.
    assert glyphs[1][1] > glyphs[0][1]
    assert_order(glyphs, 'AB')


def test_start_offset_absolute_and_clamp():
    """Absolute offsets are honored and out-of-range ones are clamped."""
    glyphs = render('''
      <path id="p" d="M10 50 L110 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p" startOffset="30">AB</textPath>
      </text>''')
    assert_point(glyphs[0], x=40, y=50)

    # An offset beyond the path end is clamped: nothing is drawn outside.
    assert render('''
      <path id="p2" d="M10 50 L60 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p2" href="#p2" startOffset="150%">AB</textPath>
      </text>''') == []

    # A negative offset is clamped to the path start.
    glyphs = render('''
      <path id="p3" d="M10 50 L110 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p3" href="#p3" startOffset="-20">AB</textPath>
      </text>''')
    assert_point(glyphs[0], x=10, y=50)


def test_text_anchor_on_path():
    """text-anchor shifts glyphs along the direction of the path."""
    x_bearing, width = text_width('AB')
    middle = render('''
      <path id="p" d="M10 50 L190 50" fill="none"/>
      <text font-family="Arial" font-size="10" text-anchor="middle">
        <textPath xlink:href="#p" href="#p" startOffset="50%">AB</textPath>
      </text>''')
    # The ink box of the text is centered on the middle of the path.
    ink_left = middle[0][1] + x_bearing
    ink_center = ink_left + width / 2
    assert abs(ink_center - 100) <= POSITION_TOLERANCE

    end = render('''
      <path id="p2" d="M10 50 L190 50" fill="none"/>
      <text font-family="Arial" font-size="10" text-anchor="end">
        <textPath xlink:href="#p2" href="#p2" startOffset="100%">AB</textPath>
      </text>''')
    # The end anchor puts the right ink edge at the path end.
    ink_right = end[0][1] + x_bearing + width
    assert abs(ink_right - 190) <= POSITION_TOLERANCE
    assert end[0][1] < end[-1][1]


def test_reverse_path_direction():
    """A path going right-to-left lays glyphs out backwards in that order."""
    glyphs = render('''
      <path id="p" d="M150 50 L50 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p">AB</textPath>
      </text>''')
    assert_point(glyphs[0], x=150, y=50, angle=180)
    # The second glyph walks towards the path start (smaller x).
    assert glyphs[1][1] < glyphs[0][1]
    assert_order(glyphs, 'AB')

    # An end anchor on the same reversed path must keep the reversed
    # direction: glyphs advance towards the path start (x=50), and the
    # whole run is shifted backwards from the path end (x=150).
    anchored = render('''
      <path id="p2" d="M150 60 L50 60" fill="none"/>
      <text font-family="Arial" font-size="10" text-anchor="end">
        <textPath xlink:href="#p2" href="#p2"
                  startOffset="100%">AB</textPath>
      </text>''')
    assert_point(anchored[0], angle=180)
    assert anchored[1][1] < anchored[0][1]
    assert all(50 - POSITION_TOLERANCE <= g[1] <= 150
               for g in anchored)


def test_glyph_offset_along_path():
    """A character dx follows the path direction."""
    glyphs = render('''
      <path id="p" d="M10 50 L190 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p"><tspan dx="20">AB</tspan>
        </textPath>
      </text>''')
    assert_point(glyphs[0], x=30, y=50, angle=0)
    assert glyphs[1][1] > glyphs[0][1]


def test_multiple_subpaths():
    """Text continues on the second subpath after crossing the first one."""
    glyphs = render('''
      <path id="p" d="M10 30 L60 30 M10 90 L200 90" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p">ABCDEFGHIJ</textPath>
      </text>''')
    assert_order(glyphs, 'ABCDEFGHIJ')
    # The first subpath holds the first glyphs at y=30...
    first_y30 = [g for g in glyphs if abs(g[2] - 30) <= POSITION_TOLERANCE]
    assert first_y30
    # ...and the remaining glyphs continue on the second subpath at y=90.
    second_y90 = [g for g in glyphs if abs(g[2] - 90) <= POSITION_TOLERANCE]
    assert second_y90
    assert first_y30[-1][0] == 'H'
    assert second_y90[0][0] == 'I'


def test_closed_curve():
    """A closed circular arc keeps a continuous, rotating direction."""
    glyphs = render('''
      <path id="c" d="M100,40 A60,60 0 1,1 99.99,40" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#c" href="#c">AROUND</textPath>
      </text>''')
    assert_order(glyphs, 'AROUND')
    # Glyphs stay on the circle (center (100, 100), radius 60).
    for letter, x, y, angle in glyphs:
        radius = math.hypot(x - 100, y - 100)
        assert abs(radius - 60) <= POSITION_TOLERANCE
    # Angles follow the arc instead of staying flat.
    assert glyphs[-1][3] > glyphs[0][3] + ANGLE_TOLERANCE


def test_zero_length_path():
    """A zero-length path draws nothing but never raises."""
    glyphs = render('''
      <path id="p" d="M10 50 L10 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p">AB</textPath>
      </text>''')
    assert glyphs == []

    # A path made only of moves has no drawable segment either.
    glyphs = render('''
      <path id="p2" d="M10 50 M40 50 M80 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p2" href="#p2">AB</textPath>
      </text>''')
    assert glyphs == []


def test_short_path_does_not_leak_to_sibling_text():
    """Overflowing textPath state must not move later normal text."""
    glyphs = render('''
      <path id="p" d="M10 50 L14 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p">TOOLONG</textPath>
      </text>
      <text x="10" y="90" font-family="Arial" font-size="10">AFTER</text>''')
    assert_order(glyphs, 'TAFTER')
    # Only the first textPath glyph fits on the 4-unit path.
    assert_point(glyphs[0], x=10, y=50)
    # The sibling text starts at its own x/y, unaffected by the overflow.
    sibling = glyphs[1:]
    assert_point(sibling[0], x=10, y=90)
    for letter, x, y, angle in sibling:
        assert angle == 0


def test_zero_length_path_does_not_leak_to_sibling_text():
    """An empty path must leave sibling text fully functional."""
    glyphs = render('''
      <path id="p" d="M10 50 L10 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p">AB</textPath>
      </text>
      <text x="10" y="90" font-family="Arial" font-size="10">SIBLING</text>''')
    assert_order(glyphs, 'SIBLING')
    assert_point(glyphs[0], x=10, y=90)


def test_path_transform_is_honored():
    """The path transform applies to the glyph positions and angles."""
    glyphs = render('''
      <g transform="translate(40 60) rotate(90)">
        <path id="p" d="M0 0 L100 0" fill="none"/>
        <text font-family="Arial" font-size="10">
          <textPath xlink:href="#p" href="#p">AB</textPath>
        </text>
      </g>''')
    assert len(glyphs) == 2
    # A horizontal line rotated by 90 degrees around (40, 60) becomes a
    # vertical line going down from that point.
    assert_point(glyphs[0], x=40, y=60, angle=90)
    assert glyphs[1][2] > glyphs[0][2]


def test_repeated_reference_keeps_geometry():
    """Referencing the same path twice gives identical layouts."""
    glyphs = render('''
      <path id="p" d="M20 40 Q100 10 180 40" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p">SAME</textPath>
      </text>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p">SAME</textPath>
      </text>''')
    # The same text referencing the same path twice must produce two
    # byte-for-byte identical layouts: the shared geometry is never mutated.
    first, second = glyphs[:4], glyphs[4:]
    assert [g[0] for g in first] == [g[0] for g in second] == list('SAME')
    assert first == second


def test_nested_tspans_keep_advancing():
    """Tspans inside one textPath continue the same path advance."""
    glyphs = render('''
      <path id="p" d="M10 50 L210 50" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p">
          <tspan>AB</tspan><tspan>CD</tspan>
        </textPath>
      </text>''')
    assert_order(glyphs, 'ABCD')
    positions = [g[1] for g in glyphs]
    assert positions == sorted(positions)


def test_polyline_with_ticks():
    """Text follows each leg of a ticked polyline with the local tangent."""
    glyphs = render('''
      <g stroke="gray">
        <path id="pl" d="M20 120 L60 60 L110 100 L160 40 L200 80"
              fill="none"/>
        <path d="M20 120 l-4 6 M60 60 l-4 6 M110 100 l-4 6
                 M160 40 l-4 6 M200 80 l-4 6"/>
      </g>
      <text font-family="Arial" font-size="12">
        <textPath xlink:href="#pl" href="#pl">POLY</textPath>
      </text>''')
    assert_order(glyphs, 'POLY')
    # The first glyph starts exactly at the first polyline vertex.
    assert_point(glyphs[0], x=20, y=120)
    # The first leg goes up-right: its tangent is a negative angle.
    expected = math.degrees(math.atan2(60 - 120, 60 - 20))
    assert abs(glyphs[0][3] - expected) <= ANGLE_TOLERANCE
    # Consecutive glyphs strictly advance along the path.
    distances = [math.hypot(g[1] - 20, g[2] - 120) for g in glyphs]
    assert distances == sorted(distances)
    assert distances[-1] > distances[0]


def test_empty_subpath_between_real_ones():
    """A zero-length subpath is skipped without losing direction."""
    glyphs = render('''
      <path id="p"
            d="M10 30 L60 30 M100 30 L100 30 M10 90 L160 90" fill="none"/>
      <text font-family="Arial" font-size="10">
        <textPath xlink:href="#p" href="#p">ABCDEFGHIJKLMN</textPath>
      </text>''')
    assert_order(glyphs, 'ABCDEFGHIJKLMN')
    on_first = [g for g in glyphs if abs(g[2] - 30) <= POSITION_TOLERANCE]
    on_second = [g for g in glyphs if abs(g[2] - 90) <= POSITION_TOLERANCE]
    assert on_first and on_second
    assert on_second[0][0] == 'I'
