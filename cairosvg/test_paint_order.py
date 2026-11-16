"""
Tests for the SVG2 ``paint-order`` property.

The property controls the relative order in which the fill, the stroke and
the markers of a shape are painted. These tests render small SVG samples to
PNG and inspect the actual pixels where the painting operations overlap.

"""

import io

import pytest

from . import svg2png
from .helpers import DEFAULT_PAINT_ORDER, paint_order

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None


needs_pillow = pytest.mark.skipif(
    Image is None, reason='Pillow is required to inspect rendered pixels')


def pixels(svg):
    """Render an SVG string and return a PIL pixel access object."""
    png = svg2png(svg.encode())
    return Image.open(io.BytesIO(png)).convert('RGBA').load()


# A thick blue stroke overlaps the yellow fill; at the sampled ring point the
# visible color depends on which operation was painted last.
FILL_STROKE_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"
     viewBox="0 0 120 120">
  <rect width="120" height="120" fill="white"/>
  <rect x="30" y="30" width="60" height="60" fill="yellow" stroke="blue"
        stroke-width="20" {attribute}/>
</svg>'''

YELLOW = (255, 255, 0, 255)
BLUE = (0, 0, 255, 255)
RED = (255, 0, 0, 255)
BLACK = (0, 0, 0, 255)

# (32, 32) is inside the stroke ring but outside the geometry's fill area,
# (40, 60) is covered by both the stroke and the fill.
RING_POINT = (32, 32)
OVERLAP_POINT = (40, 60)
CENTER_POINT = (60, 60)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('value, expected', [
    (None, ('fill', 'stroke', 'markers')),
    ('', ('fill', 'stroke', 'markers')),
    ('normal', ('fill', 'stroke', 'markers')),
    ('fill', ('fill', 'stroke', 'markers')),
    ('stroke', ('stroke', 'fill', 'markers')),
    ('markers', ('markers', 'fill', 'stroke')),
    ('stroke fill', ('stroke', 'fill', 'markers')),
    ('markers stroke fill', ('markers', 'stroke', 'fill')),
    ('fill markers', ('fill', 'markers', 'stroke')),
    # Commas and extra whitespace are tolerated
    ('stroke,  fill', ('stroke', 'fill', 'markers')),
    # Case-insensitive keywords (presentation attributes are not lowercased)
    ('STROKE Fill', ('stroke', 'fill', 'markers')),
    # Invalid declarations fall back to the default order, they never raise
    ('fill fill', DEFAULT_PAINT_ORDER),
    ('stroke normal', DEFAULT_PAINT_ORDER),
    ('nonsense', DEFAULT_PAINT_ORDER),
    ('stroke nonsense fill', DEFAULT_PAINT_ORDER),
])
def test_paint_order_parser(value, expected):
    assert paint_order(value) == expected
    # Every returned order contains each operation exactly once
    assert sorted(paint_order(value)) == sorted(DEFAULT_PAINT_ORDER)


# ---------------------------------------------------------------------------
# Rendering: default order vs stroke first
# ---------------------------------------------------------------------------

@needs_pillow
def test_default_order_paints_stroke_over_fill():
    px = pixels(FILL_STROKE_SVG.format(attribute=''))
    assert px[RING_POINT] == BLUE
    assert px[OVERLAP_POINT] == YELLOW
    assert px[CENTER_POINT] == YELLOW


@needs_pillow
def test_stroke_first_paints_fill_over_stroke():
    px = pixels(FILL_STROKE_SVG.format(
        attribute='paint-order="stroke"'))
    # The fill is painted after the stroke and covers it where they overlap
    assert px[RING_POINT] == YELLOW
    assert px[OVERLAP_POINT] == YELLOW
    assert px[CENTER_POINT] == YELLOW


@needs_pillow
def test_explicit_normal_matches_default():
    default = pixels(FILL_STROKE_SVG.format(attribute=''))
    normal = pixels(FILL_STROKE_SVG.format(
        attribute='paint-order="normal"'))
    assert default[RING_POINT] == normal[RING_POINT] == BLUE


@needs_pillow
def test_shorthand_appends_missing_operations_in_default_position():
    # "stroke fill" lists no markers: markers are appended last, while the
    # stroke still happens before the fill.
    px = pixels(FILL_STROKE_SVG.format(
        attribute='paint-order="stroke fill"'))
    assert px[RING_POINT] == YELLOW


@needs_pillow
def test_full_explicit_order():
    px = pixels(FILL_STROKE_SVG.format(
        attribute='paint-order="markers stroke fill"'))
    assert px[RING_POINT] == YELLOW


@needs_pillow
def test_invalid_value_keeps_node_visible_with_default_order():
    px = pixels(FILL_STROKE_SVG.format(
        attribute='paint-order="stroke; garbage"'))
    # The invalid declaration is ignored: default rendering, node still there
    assert px[RING_POINT] == BLUE
    assert px[CENTER_POINT] == YELLOW


@needs_pillow
def test_duplicate_keyword_falls_back_to_default_order():
    px = pixels(FILL_STROKE_SVG.format(
        attribute='paint-order="markers markers"'))
    assert px[RING_POINT] == BLUE
    assert px[CENTER_POINT] == YELLOW


@needs_pillow
def test_empty_value_uses_default_order():
    px = pixels(FILL_STROKE_SVG.format(attribute='paint-order=""'))
    assert px[RING_POINT] == BLUE


@needs_pillow
def test_paint_order_in_style_attribute():
    px = pixels(FILL_STROKE_SVG.format(
        attribute='style="paint-order: stroke"'))
    assert px[RING_POINT] == YELLOW


@needs_pillow
def test_paint_order_in_css_stylesheet():
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120"
         viewBox="0 0 120 120">
      <style>.stroke-first { paint-order: stroke; }</style>
      <rect width="120" height="120" fill="white"/>
      <rect class="stroke-first" x="30" y="30" width="60" height="60"
            fill="yellow" stroke="blue" stroke-width="20"/>
    </svg>'''
    px = pixels(svg)
    assert px[RING_POINT] == YELLOW


@needs_pillow
def test_none_fill_and_none_stroke_keep_working():
    no_fill = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"
         viewBox="0 0 80 80">
      <rect width="80" height="80" fill="white"/>
      <rect x="15" y="15" width="50" height="50" fill="none" stroke="blue"
            stroke-width="10" paint-order="markers stroke fill"/>
    </svg>'''
    px = pixels(no_fill)
    assert px[(15, 40)] == BLUE       # stroke still painted
    assert px[(40, 40)] == (255, 255, 255, 255)  # nothing inside

    no_stroke = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"
         viewBox="0 0 80 80">
      <rect width="80" height="80" fill="white"/>
      <rect x="15" y="15" width="50" height="50" fill="yellow"
            stroke="none" stroke-width="10" paint-order="stroke"/>
    </svg>'''
    px = pixels(no_stroke)
    assert px[(18, 40)] == YELLOW     # no blue ring drawn


# ---------------------------------------------------------------------------
# Rendering: markers ordering
# ---------------------------------------------------------------------------

MARKER_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="120"
     viewBox="0 0 200 120">
  <rect width="200" height="120" fill="white"/>
  <path d="M20 60 H180" fill="none" stroke="black" stroke-width="24"
        marker-start="url(#m)" marker-end="url(#m)" {attribute}/>
  <defs>
    <marker id="m" markerWidth="8" markerHeight="8" refX="4" refY="4"
            markerUnits="userSpaceOnUse">
      <rect x="0" y="0" width="8" height="8" fill="red"/>
    </marker>
  </defs>
</svg>'''

# Center of the start marker, which sits on top of the thick black stroke
MARKER_POINT = (20, 60)
LINE_POINT = (100, 60)


@needs_pillow
def test_default_order_paints_markers_over_stroke():
    px = pixels(MARKER_SVG.format(attribute=''))
    assert px[MARKER_POINT] == RED
    assert px[LINE_POINT] == BLACK


@needs_pillow
def test_markers_first_paints_stroke_over_markers():
    px = pixels(MARKER_SVG.format(
        attribute='paint-order="markers"'))
    # Stroke painted after the marker hides it at the endpoint
    assert px[MARKER_POINT] == BLACK
    # The stroke itself is of course still drawn
    assert px[LINE_POINT] == BLACK


@needs_pillow
def test_stroke_first_keeps_markers_on_top_by_default_marker_position():
    # "stroke" alone moves the stroke before the fill, but the omitted
    # markers keep their default (last) position.
    px = pixels(MARKER_SVG.format(
        attribute='paint-order="stroke"'))
    assert px[MARKER_POINT] == RED
    assert px[LINE_POINT] == BLACK


# ---------------------------------------------------------------------------
# Inheritance and local overrides
# ---------------------------------------------------------------------------

INHERITANCE_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="240" height="80"
     viewBox="0 0 240 80">
  <rect width="240" height="80" fill="white"/>
  <g paint-order="stroke">
    <rect x="5" y="20" width="60" height="40" fill="yellow" stroke="blue"
          stroke-width="12"/>
    <rect x="85" y="20" width="60" height="40" fill="yellow" stroke="blue"
          stroke-width="12" paint-order="normal"/>
    <rect x="165" y="20" width="60" height="40" fill="yellow" stroke="blue"
          stroke-width="12" style="paint-order: markers stroke fill"/>
  </g>
</svg>'''


@needs_pillow
def test_paint_order_is_inherited_and_can_be_overridden():
    px = pixels(INHERITANCE_SVG)
    # First rect inherits stroke-first from the group
    assert px[(7, 40)] == YELLOW
    # Second rect explicitly restores the default order
    assert px[(87, 40)] == BLUE
    # Third rect uses a style attribute to go back to stroke-first
    assert px[(167, 40)] == YELLOW


# ---------------------------------------------------------------------------
# Path preservation, state isolation and combinations
# ---------------------------------------------------------------------------

@needs_pillow
def test_multiple_subpaths_are_all_painted():
    # A previous painting operation must not clear the path before the next
    # one: both subpaths need their fill and their stroke.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="160" height="80"
         viewBox="0 0 160 80">
      <rect width="160" height="80" fill="white"/>
      <path d="M20 20 h40 v40 h-40 z M100 20 h40 v40 h-40 z"
            fill="yellow" stroke="blue" stroke-width="12"
            paint-order="stroke"/>
    </svg>'''
    px = pixels(svg)
    for center in ((40, 40), (120, 40)):
        assert px[center] == YELLOW
    # Stroke ring of each subpath is covered by the later fill
    assert px[(22, 40)] == YELLOW
    assert px[(102, 40)] == YELLOW


@needs_pillow
def test_paint_order_does_not_leak_between_sibling_nodes():
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="180" height="80"
         viewBox="0 0 180 80">
      <rect width="180" height="80" fill="white"/>
      <rect x="10" y="20" width="60" height="40" fill="yellow"
            stroke="blue" stroke-width="12" paint-order="stroke"/>
      <rect x="100" y="20" width="60" height="40" fill="yellow"
            stroke="blue" stroke-width="12"/>
    </svg>'''
    px = pixels(svg)
    # First rect: fill over stroke; second rect must keep the default order
    assert px[(12, 40)] == YELLOW
    assert px[(102, 40)] == BLUE


@needs_pillow
def test_stroke_first_with_gradient_fill():
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"
         viewBox="0 0 80 80">
      <defs>
        <linearGradient id="g">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="lime"/>
        </linearGradient>
      </defs>
      <rect width="80" height="80" fill="white"/>
      <rect x="20" y="20" width="40" height="40" fill="url(#g)"
            stroke="black" stroke-width="12" paint-order="stroke"/>
    </svg>'''
    px = pixels(svg)
    # The gradient fill covers the black stroke in the overlap ring
    assert px[(22, 40)][3] == 255
    assert px[(22, 40)][1] > 0  # green component of the gradient, not black


@needs_pillow
def test_stroke_first_with_clip_path():
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"
         viewBox="0 0 80 80">
      <defs>
        <clipPath id="c">
          <rect x="10" y="10" width="60" height="60"/>
        </clipPath>
      </defs>
      <rect width="80" height="80" fill="white"/>
      <rect x="20" y="20" width="60" height="60" fill="yellow"
            stroke="blue" stroke-width="12" paint-order="stroke"
            clip-path="url(#c)"/>
    </svg>'''
    px = pixels(svg)
    # Painting order honored inside the clip
    assert px[(22, 40)] == YELLOW
    # Everything outside the clip path is untouched (white background)
    assert px[(8, 8)] == (255, 255, 255, 255)


@needs_pillow
def test_stroke_first_inside_opacity_group():
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"
         viewBox="0 0 80 80">
      <rect width="80" height="80" fill="white"/>
      <g opacity="0.5">
        <rect x="20" y="20" width="40" height="40" fill="yellow"
              stroke="blue" stroke-width="12" paint-order="stroke"/>
      </g>
    </svg>'''
    px = pixels(svg)
    # The whole group is rendered to an offscreen group and composited with
    # 50% opacity; the fill-on-top order gives a blended yellow at the ring
    r, g, b, a = px[(22, 40)]
    assert a == 255
    assert r > 200 and g > 200 and b < 200


@needs_pillow
def test_stroke_first_with_text():
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="80"
         viewBox="0 0 200 80">
      <rect width="200" height="80" fill="white"/>
      <text x="20" y="55" font-size="40" fill="yellow" stroke="blue"
            stroke-width="4" {attribute}>AB</text>
    </svg>'''

    def blue_pixels(attribute):
        px = pixels(svg.format(attribute=attribute))
        return sum(
            1 for x in range(15, 110) for y in range(15, 65)
            if px[x, y][2] > 180 and px[x, y][0] < 80)

    default = blue_pixels('')
    stroke_first = blue_pixels('paint-order="stroke"')
    # With the stroke painted first, the fill hides most of it
    assert stroke_first < default
