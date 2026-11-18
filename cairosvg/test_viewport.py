"""
Pixel-level tests for nested viewport length resolution and state restore.

Coverage of the viewport/percent rules:

* percentages resolve against the effective viewport of the element they
  belong to, at every nesting level;
* viewBox with a non-zero origin and preserveAspectRatio (meet / slice /
  none) map user units to device units correctly, including letterboxes;
* a node without size or with an empty viewport never inherits the size of
  a previous sibling, and leaving a nested ``svg`` restores the parent
  coordinate system for the following siblings;
* ``symbol`` referenced through ``use`` gives the same result as the
  equivalent directly nested ``svg``, including single-dimension sizing,
  viewBox on the use element and repeated references;
* drawing the same parsed tree more than once is idempotent.

Probe pixels are interior or exterior points chosen by analytic geometry,
never antialiased edges, so exact RGB comparisons are deterministic.

"""

import io

import pytest

from . import svg2png
from .parser import Tree
from .surface import PNGSurface

WHITE = (255, 255, 255)
RED = (255, 0, 0)
GREEN = (0, 128, 0)
BLUE = (0, 0, 255)
BLACK = (0, 0, 0)


def pixels(svg_source):
    """Render ``svg_source`` and return ``(image, pixel_accessor)``."""
    from PIL import Image
    png = svg2png(svg_source.encode() if isinstance(svg_source, str)
                  else svg_source)
    image = Image.open(io.BytesIO(png)).convert('RGB')
    return image, image.load()


def assert_pixels(accessor, expected):
    """Assert that each ``(x, y): color`` probe matches exactly."""
    for (x, y), color in expected.items():
        assert accessor[x, y] == color, (
            f'pixel ({x}, {y}) = {accessor[x, y]}, expected {color}')


def render_twice(svg_source):
    """Parse once, render twice, return the two PNG byte strings."""
    tree = Tree(bytestring=(
        svg_source.encode() if isinstance(svg_source, str) else svg_source))
    outputs = []
    for _ in range(2):
        buffer = io.BytesIO()
        surface = PNGSurface(tree, buffer, 96)
        surface.finish()
        outputs.append(buffer.getvalue())
    return outputs


# ---------------------------------------------------------------------------
# Percentage resolution against the effective viewport
# ---------------------------------------------------------------------------

def test_percent_width_height_on_nested_svg():
    """width/height percentages on a nested svg use the parent viewport."""
    # 50% x 40% of a 100x100 parent => 50x40 viewport at (10,10).
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="10" y="10" width="50%" height="40%">
        <rect width="100%" height="100%" fill="red"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (11, 11): RED,
        (59, 49): RED,
        (60, 30): WHITE,
        (30, 50): WHITE,
    })


def test_percent_shapes_inside_nested_viewport():
    """Percentages on shapes inside a nested svg use the nested viewport."""
    # Nested viewport 80x40 at (20,10), no viewBox: 1 user unit == 1 px.
    # rect 50% x 50% => 40x20; circle cx/cy 50% => center (60,30),
    # r 25% of the normalized diagonal => round circle centered there.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <rect width="200" height="100" fill="white"/>
      <svg x="20" y="10" width="80" height="40">
        <rect x="0" y="0" width="50%" height="50%" fill="red"/>
        <circle cx="50%" cy="50%" r="25%" fill="blue"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    # The rect fills the top-left 40x20 of the nested viewport; probe its
    # corner away from the circle.
    assert_pixels(px, {
        (22, 12): RED,
        (25, 25): RED,
        (60, 30): BLUE,   # exact circle center
        (60, 12): WHITE,  # above the circle
        (60, 48): WHITE,  # below the circle (viewport ends at y=49)
    })


def test_percent_resolution_at_each_nesting_level():
    """A percentage inside a deeply nested svg never uses the outer canvas."""
    # Outer 200x200; middle 50%x50% => 100x100; the inner circle r=50%
    # resolves against the 100x100 middle viewport => radius 50.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
      <rect width="200" height="200" fill="white"/>
      <svg x="0" y="0" width="50%" height="50%">
        <circle cx="50%" cy="50%" r="50%" fill="blue"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (50, 50): BLUE,
        (2, 50): BLUE,
        (98, 50): BLUE,
        (100, 50): WHITE,
    })


def test_percent_stroke_width_uses_nested_viewport():
    """A percentage stroke-width resolves against the active viewport."""
    # Inner viewport 40x40: 10% of its normalized diagonal (40) => width 4.
    # The same 10% on the outer 200x200 canvas gives width 20.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
      <rect width="200" height="200" fill="white"/>
      <svg x="20" y="20" width="40" height="40">
        <line x1="0" y1="20" x2="40" y2="20" stroke="black"
              stroke-width="10%"/>
      </svg>
      <line x1="0" y1="100" x2="40" y2="100" stroke="black"
            stroke-width="10%"/>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (40, 38): BLACK, (40, 41): BLACK,
        (40, 37): WHITE, (40, 42): WHITE,
        (20, 90): BLACK, (20, 109): BLACK,
        (20, 89): WHITE, (20, 110): WHITE,
    })


# ---------------------------------------------------------------------------
# viewBox, non-zero origin and preserveAspectRatio mapping
# ---------------------------------------------------------------------------

def test_nonzero_viewbox_origin_mapping():
    """Percentages and coordinates map through a non-zero viewBox origin."""
    # Viewport 80x80, viewBox "20 20 40 40" => scale 2; user (20,20) maps
    # to device (0,0). A rect placed at the viewBox origin whose size is
    # 50% of the viewBox (20 user units) fills the 40x40 device box. A
    # circle with cx/cy 50% gets center 20 user units (half of the viewBox
    # width, measured from user origin 0) => device (0,0), radius 25% =>
    # 10 user units => 20 device px, clipped by the viewport.
    svg_rect = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
      <rect width="80" height="80" fill="white"/>
      <svg x="0" y="0" width="80" height="80" viewBox="20 20 40 40">
        <rect x="20" y="20" width="50%" height="50%" fill="blue"/>
      </svg>
    </svg>'''
    _, px = pixels(svg_rect)
    assert_pixels(px, {
        (1, 1): BLUE,
        (38, 38): BLUE,
        (40, 40): WHITE,
    })

    svg_circle = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
      <rect width="80" height="80" fill="white"/>
      <svg x="0" y="0" width="80" height="80" viewBox="20 20 40 40">
        <circle cx="50%" cy="50%" r="25%" fill="blue"/>
      </svg>
    </svg>'''
    _, px = pixels(svg_circle)
    assert_pixels(px, {
        (1, 1): BLUE,
        (10, 10): BLUE,
        (19, 19): WHITE,
        (40, 40): WHITE,
    })


def test_meet_letterbox_centers_content():
    """meet keeps the aspect ratio and centers content in the viewport."""
    # Viewport 100x200, square viewBox 100x100 => scale 1, content centered
    # vertically with a 50px letterbox above and below.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="200">
      <rect width="100" height="200" fill="white"/>
      <svg x="0" y="0" width="100" height="200" viewBox="0 0 100 100"
           preserveAspectRatio="xMidYMid meet">
        <rect width="100" height="100" fill="red"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (50, 50): RED,
        (50, 149): RED,
        (50, 49): WHITE,
        (50, 150): WHITE,
    })


def test_slice_fills_viewport_and_clips():
    """slice scales to cover the viewport and clips the overflow."""
    # Viewport 200x100, square viewBox => scale 2, the 100-unit square
    # becomes 200x200 centered horizontally, clipped to the 100px height.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <rect width="200" height="100" fill="white"/>
      <svg x="0" y="0" width="200" height="100" viewBox="0 0 100 100"
           preserveAspectRatio="xMidYMid slice">
        <rect width="100" height="100" fill="red"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (100, 1): RED,
        (100, 98): RED,
        (1, 50): RED,
        (199, 50): RED,
    })


def test_preserve_aspect_ratio_none_stretches():
    """none scales the axes independently."""
    # Viewport 80x40, viewBox 0 0 40 40 => sx 2, sy 1; a full-viewBox rect
    # fills the whole viewport and a 25%-radius circle becomes an ellipse.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <rect width="200" height="100" fill="white"/>
      <svg x="20" y="10" width="80" height="40" viewBox="0 0 40 40"
           preserveAspectRatio="none">
        <rect width="40" height="40" fill="red"/>
        <circle cx="50%" cy="50%" r="25%" fill="blue"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (21, 11): RED,
        (99, 49): RED,
        (60, 30): BLUE,
        (60, 21): BLUE,   # 10 user units * sy 1 => 9 device px above center
        (60, 19): RED,
    })


# ---------------------------------------------------------------------------
# State restore when leaving a nested viewport
# ---------------------------------------------------------------------------

def test_sibling_percentage_after_nested_svg():
    """Percentages on a sibling use the restored parent viewport."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="0" y="0" width="40" height="40" viewBox="0 0 8 8">
        <rect width="8" height="8" fill="red"/>
      </svg>
      <rect x="60" y="0" width="50%" height="50%" fill="blue"/>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (20, 20): RED,
        (45, 20): WHITE,
        (61, 20): BLUE,
        (60, 49): BLUE,
        (60, 50): WHITE,
    })


def test_sibling_order_does_not_change_rendering():
    """Swapping the nested svg and its sibling produces the same pixels."""
    nested = (
        '<svg x="40" y="40" width="80" height="80" viewBox="0 0 20 20">'
        '<line x1="0" y1="10" x2="20" y2="10" stroke="red" '
        'stroke-width="2"/></svg>')
    line = ('<line x1="0" y1="150" x2="100" y2="150" stroke="blue" '
            'stroke-width="4"/>')
    svg1 = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
      <rect width="200" height="200" fill="white"/>
      {nested}{line}
    </svg>'''
    svg2 = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
      <rect width="200" height="200" fill="white"/>
      {line}{nested}
    </svg>'''
    image1, _ = pixels(svg1)
    image2, _ = pixels(svg2)
    assert image1.tobytes() == image2.tobytes()


def test_deeply_nested_viewport_restore():
    """Transform, viewport size and clipping are restored at every level."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="10" y="10" width="80" height="80" viewBox="0 0 80 80">
        <rect width="80" height="80" fill="#eeeeee"/>
        <svg x="10" y="10" width="40" height="40" viewBox="0 0 20 20">
          <rect x="-10" y="-10" width="40" height="40" fill="red"/>
          <rect width="20" height="20" fill="blue"/>
        </svg>
        <rect x="10" y="55" width="50%" height="10" fill="green"/>
      </svg>
      <rect x="0" y="92" width="50%" height="8" fill="black"/>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (21, 21): BLUE,
        (19, 20): (238, 238, 238),
        (60, 20): (238, 238, 238),
        (30, 66): GREEN,
        (59, 70): GREEN,
        (60, 70): (238, 238, 238),
        (25, 95): BLACK,
        (49, 95): BLACK,
        (50, 95): WHITE,
    })


def test_empty_viewport_node_does_not_leak_size():
    """A zero-size nested svg leaves the viewport of its siblings intact."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect width="120" height="120" fill="white"/>
      <svg x="0" y="0" width="60" height="60" viewBox="0 0 30 30">
        <svg width="0" height="0"/>
        <circle cx="50%" cy="50%" r="50%" fill="blue"/>
      </svg>
      <rect x="60" y="0" width="50%" height="50%" fill="red"/>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (30, 30): BLUE,
        (2, 30): BLUE,
        (58, 30): BLUE,
        (61, 1): RED,
        (118, 58): RED,
        (90, 60): WHITE,
    })


def test_zero_viewbox_dimension_does_not_crash():
    """A viewBox with a zero dimension renders an empty element, no crash."""
    for viewbox in ('0 0 0 10', '0 0 10 0', '0 0 0 0'):
        svg = f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <rect width="100" height="100" fill="white"/>
          <svg x="0" y="0" width="50" height="50" viewBox="{viewbox}">
            <rect width="10" height="10" fill="blue"/>
          </svg>
          <rect x="60" y="60" width="20" height="20" fill="red"/>
        </svg>'''
        _, px = pixels(svg)
        assert px[70, 70] == RED


# ---------------------------------------------------------------------------
# symbol / use parity and sizing
# ---------------------------------------------------------------------------

SYMBOL_PARITY_CIRCLE = (
    '<circle cx="50%" cy="50%" r="50%" fill="blue"/>')


def test_symbol_use_matches_direct_nested_svg():
    """A used symbol and the equivalent nested svg render identically."""
    svg_use = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
      <defs>
        <symbol id="s" viewBox="0 0 20 20">{SYMBOL_PARITY_CIRCLE}</symbol>
      </defs>
      <use href="#s" x="10" y="10" width="40" height="40"/>
    </svg>'''
    svg_nested = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
      <svg x="10" y="10" width="40" height="40" viewBox="0 0 20 20">
        {SYMBOL_PARITY_CIRCLE}
      </svg>
    </svg>'''
    image_use, _ = pixels(svg_use)
    image_nested, _ = pixels(svg_nested)
    assert image_use.tobytes() == image_nested.tobytes()


def test_symbol_reused_twice_different_sizes():
    """Two references of one symbol each use their own geometry."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
          <rect x="2" y="2" width="6" height="6" fill="blue"/>
        </symbol>
      </defs>
      <rect width="120" height="80" fill="white"/>
      <use href="#s" x="5" y="5" width="30" height="30"/>
      <use href="#s" x="45" y="10" width="60" height="40"/>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (20, 20): BLUE,
        (70, 40): BLUE,
        (54, 20): WHITE,
        (95, 30): WHITE,
        (70, 9): WHITE,
        (70, 50): WHITE,
    })


def test_use_single_width_dimension():
    """A width-only use keeps 100% height resolved through the viewport."""
    # Symbol viewBox 0 0 10 20, parent 100x100, width 40 => viewport 40x100,
    # meet scale 4, content 40x80 centered vertically (y 10..89).
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <symbol id="s" viewBox="0 0 10 20">
          <rect width="10" height="20" fill="red"/>
        </symbol>
      </defs>
      <rect width="100" height="100" fill="white"/>
      <use href="#s" x="0" y="0" width="40"/>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (5, 15): RED,
        (35, 85): RED,
        (40, 50): WHITE,
        (20, 9): WHITE,
        (20, 90): WHITE,
    })


def test_use_single_height_dimension():
    """A height-only use keeps 100% width resolved through the viewport."""
    # Viewport 100x40 at y=30, meet scale 2, content 20x40 centered
    # horizontally (x 40..59).
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <symbol id="s" viewBox="0 0 10 20">
          <rect width="10" height="20" fill="red"/>
        </symbol>
      </defs>
      <rect width="100" height="100" fill="white"/>
      <use href="#s" x="0" y="30" height="40"/>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (45, 35): RED,
        (55, 65): RED,
        (39, 50): WHITE,
        (60, 50): WHITE,
        (50, 29): WHITE,
        (50, 70): WHITE,
    })


def test_use_viewbox_attribute_is_applied():
    """A viewBox carried by the use element establishes its viewport."""
    # Without the viewBox the 25-unit rect would paint 25px; with
    # viewBox 0 0 50 50 mapped on the 80px viewport (scale 1.6) it fills it.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
      <defs>
        <symbol id="s">
          <rect width="25" height="25" fill="blue"/>
        </symbol>
      </defs>
      <rect width="200" height="100" fill="white"/>
      <use href="#s" x="10" y="10" width="80" height="80"
           viewBox="0 0 50 50"/>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (11, 11): BLUE,
        (49, 49): BLUE,
        (50, 30): WHITE,
    })


def test_use_of_group_ignores_geometry_attributes():
    """width/height on a use of a plain group keep historical behavior."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="60">
      <defs>
        <g id="g">
          <rect width="20" height="20" fill="red"/>
        </g>
      </defs>
      <rect width="100" height="60" fill="white"/>
      <use href="#g" x="10" y="10"/>
      <use href="#g" x="40" y="20" width="50" height="50"/>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (11, 11): RED,
        (41, 21): RED,
        (59, 39): RED,
        (60, 30): WHITE,
    })


# ---------------------------------------------------------------------------
# Idempotence: rendering one parsed tree repeatedly
# ---------------------------------------------------------------------------

def test_repeated_render_of_use_is_idempotent():
    """The use node must not be mutated by the first render."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="blue"/>
        </symbol>
      </defs>
      <use href="#s" x="10" y="10" width="40" height="40"/>
    </svg>'''
    first, second = render_twice(svg)
    assert first == second


def test_repeated_render_of_masked_use_is_idempotent():
    """The mask definition must not be mutated by the first render."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <mask id="m">
          <rect width="100" height="50" fill="white"/>
        </mask>
        <rect id="r" width="100" height="100" fill="blue"/>
      </defs>
      <use href="#r" mask="url(#m)"/>
    </svg>'''
    first, second = render_twice(svg)
    assert first == second


def test_repeated_render_of_pattern_is_idempotent():
    """The pattern definition must not be mutated by the first render."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>
        <pattern id="p" width="10" height="10"
                 patternUnits="userSpaceOnUse">
          <rect width="10" height="10" fill="white"/>
          <rect width="5" height="5" fill="blue"/>
        </pattern>
      </defs>
      <rect width="100" height="100" fill="url(#p)"/>
    </svg>'''
    first, second = render_twice(svg)
    assert first == second


def test_repeated_render_of_embedded_svg_image_is_idempotent():
    """An embedded SVG image must not be moved by a second render."""
    import base64
    inner = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
        b'<rect width="40" height="40" fill="blue"/></svg>')
    uri = 'data:image/svg+xml;base64,' + base64.b64encode(inner).decode()
    svg = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">
      <rect width="120" height="80" fill="white"/>
      <image href="{uri}" x="20" y="10" width="40" height="40"/>
    </svg>'''
    first, second = render_twice(svg)
    assert first == second
    image, px = pixels(svg)
    assert px[20, 10] == BLUE
    assert px[59, 49] == BLUE
    assert px[60, 30] == WHITE


@pytest.mark.parametrize('overflow,outside_color', (
    ('hidden', WHITE),
    ('visible', RED),
))
def test_nested_svg_overflow_attribute(overflow, outside_color):
    """overflow=hidden and visible differ for out-of-viewport content."""
    svg = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="20" y="20" width="40" height="40" viewBox="0 0 10 10"
           overflow="{overflow}">
        <rect x="-5" y="-5" width="20" height="20" fill="red"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert px[2, 2] == outside_color
    assert px[40, 40] == RED
