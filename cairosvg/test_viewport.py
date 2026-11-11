"""
Pixel-level regression tests for viewport handling of nested ``svg`` elements
and ``use`` references to ``symbol``/``svg`` elements.

All test images are small, use plain colors and no fonts. The checked pixels
are interior or exterior points chosen with analytic geometry: they never lie
on an antialiased edge, so exact RGBA comparisons are deterministic across
Cairo versions.

Colors:
    white  = (255, 255, 255)
    red    = (255, 0, 0)
    green  = (0, 128, 0)
    blue   = (0, 0, 255)

"""

import io

import pytest

from . import svg2png

WHITE = (255, 255, 255)
RED = (255, 0, 0)
GREEN = (0, 128, 0)
BLUE = (0, 0, 255)


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


def test_nested_svg_position_and_clip():
    """Nested svg is positioned at x/y in parent coords and clipped to its
    viewport (default overflow=hidden)."""
    # Outer 100x100. Nested viewport at (10,10), 50x40, viewBox 0 0 10 10
    # => user scale 5 in x, 4 in y. Red rect covers user space (-5,-5)..(15,15)
    # and must be clipped to the 50x40 viewport.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="10" y="10" width="50" height="40" viewBox="0 0 10 10">
        <rect x="-5" y="-5" width="20" height="20" fill="red"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        # Interior of the nested viewport: red.
        (11, 11): RED,
        (59, 49): RED,
        # One pixel outside each viewport edge: clipped, background shows.
        (9, 30): WHITE,
        (60, 30): WHITE,
        (30, 9): WHITE,
        (30, 50): WHITE,
    })


def test_nested_svg_overflow_visible():
    """overflow=visible lets content paint outside the nested viewport."""
    # Viewport at (20,20), 40x40. Red rect in user space (-5,-5)..(15,15),
    # i.e. device (0,0)..(80,80) with scale 4.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="20" y="20" width="40" height="40" viewBox="0 0 10 10"
           overflow="visible">
        <rect x="-5" y="-5" width="20" height="20" fill="red"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (2, 2): RED,      # outside viewport, visible overflow
        (78, 78): RED,    # outside viewport, visible overflow
        (95, 95): WHITE,  # beyond the rect itself
        (40, 40): RED,    # inside viewport
    })


def test_percentage_sibling_viewport_restore():
    """Percentages on a sibling after a nested svg resolve against the parent
    viewport, not the nested one."""
    # Nested viewport is 40x40 (viewBox 8x8). The sibling rect uses 50%:
    # 50% of the outer 100x100 viewport = 50px, at x=60.
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
        (45, 20): WHITE,   # gap between the two rects
        (61, 20): BLUE,    # sibling starts at x=60
        (99, 20): BLUE,    # 50% width -> ends at x=110, clipped at 100
        (60, 49): BLUE,    # 50% height -> ends at y=50
        (60, 50): WHITE,
        (90, 80): WHITE,   # far below the percentage rect
    })


def test_nested_svg_percentage_size():
    """A nested svg width/height given as percentages uses the parent
    viewport as reference."""
    # 50% x 40% of 100x100 => 50x40 viewport at (10,10), viewBox 0 0 10 10
    # meet => scale 4, letterbox 5px on each side horizontally. The red rect
    # therefore covers device x 15..54, y 10..49; the (unpainted) viewport
    # spans x 10..59, y 10..49.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="10" y="10" width="50%" height="40%" viewBox="0 0 10 10">
        <rect width="10" height="10" fill="red"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (16, 11): RED,
        (54, 49): RED,
        (14, 30): WHITE,   # horizontal meet letterbox inside the viewport
        (55, 30): WHITE,   # just outside the viewport right edge
        (30, 50): WHITE,   # just below the viewport
    })


def test_symbol_reused_twice_different_sizes():
    """The same symbol referenced twice uses each use's own geometry; the
    first reference must not pollute the second."""
    # Symbol viewBox 0 0 10 10.
    # use #1: (5,5), 30x30, scale 3.  use #2: (45,10), 60x40, meet scale 4.
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
        # First instance: red ring and blue core at scale 3.
        (6, 6): RED,
        (20, 20): BLUE,
        (34, 34): RED,
        # Second instance: meet scale 4, horizontally centered
        # (10px letterbox left and right), content x 55..94, y 10..49.
        (55, 20): RED,
        (54, 20): WHITE,   # letterbox left of the meet content
        (94, 30): RED,
        (95, 30): WHITE,   # letterbox right of the meet content
        (70, 40): BLUE,
        (70, 9): WHITE,    # above the second viewport (starts at y=10)
        (70, 50): WHITE,   # below the second viewport (ends at y=49)
    })


def test_use_single_dimension():
    """Only one of width/height on a use still applies; the missing dimension
    is resolved through the standard viewport defaults (100% of the parent
    viewport) instead of the whole use being ignored."""
    # Symbol viewBox 0 0 10 20, 100x100 parent viewport.
    symbol = (
        '<symbol id="s" viewBox="0 0 10 20">'
        '<rect width="10" height="20" fill="red"/></symbol>')
    # Width only: viewport 40x100, meet scale 4 -> content 40x80 centered
    # vertically (y 10..89).
    svg_w = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>{symbol}</defs>
      <rect width="100" height="100" fill="white"/>
      <use href="#s" x="0" y="0" width="40"/>
    </svg>'''
    _, px = pixels(svg_w)
    assert_pixels(px, {
        (5, 15): RED,
        (35, 85): RED,
        (40, 50): WHITE,    # right of the 40px wide viewport
        (20, 9): WHITE,     # meet letterbox above the content
        (20, 90): WHITE,    # meet letterbox below
    })
    # Height only: viewport 100x40 at y=30, meet scale 2 -> content 20x40
    # centered horizontally (x 40..59).
    svg_h = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <defs>{symbol}</defs>
      <rect width="100" height="100" fill="white"/>
      <use href="#s" x="0" y="30" height="40"/>
    </svg>'''
    _, px = pixels(svg_h)
    assert_pixels(px, {
        (45, 35): RED,
        (55, 65): RED,
        (39, 50): WHITE,    # letterbox left of the centered content
        (60, 50): WHITE,    # letterbox right
        (50, 29): WHITE,    # above the viewport (starts at y=30)
        (50, 70): WHITE,    # below the viewport (ends at y=69)
    })


def test_use_of_svg_single_dimension():
    """Single-dimension override also applies when referencing an svg element,
    keeping its intrinsic size for the missing dimension."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">
      <defs>
        <svg id="s" width="20" height="20" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </svg>
      </defs>
      <rect width="120" height="80" fill="white"/>
      <use href="#s" x="5" y="5" width="40"/>
      <use href="#s" x="55" y="5" height="30"/>
      <use href="#s" x="90" y="5"/>
    </svg>'''
    _, px = pixels(svg)
    # First use: width 40, intrinsic height 20; meet scale 2 centers the
    # 20x20 content horizontally -> device x 15..34, y 5..24.
    assert_pixels(px, {
        (16, 7): RED,
        (33, 23): RED,
        (14, 15): WHITE,
        (35, 15): WHITE,
        (25, 25): WHITE,
        # Second use: intrinsic width 20, height 30; meet scale 2 centers
        # vertically -> x 55..74, y 10..29.
        (56, 11): RED,
        (73, 28): RED,
        (65, 9): WHITE,
        (65, 30): WHITE,
        (75, 20): WHITE,
        # Third use, no override: original 20x20 at (90,5).
        (92, 7): RED,
        (108, 23): RED,
        (110, 15): WHITE,
    })


def test_preserve_aspect_ratio_meet_slice_none():
    """meet, slice and none produce the expected geometry and clipping."""
    # Three rectangular viewports, each with a square 0 0 10 10 viewBox.
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="150" height="80">
      <rect width="150" height="80" fill="white"/>
      <svg x="2" y="2" width="24" height="40" viewBox="0 0 10 10"
           preserveAspectRatio="xMidYMid meet">
        <rect width="10" height="10" fill="red"/>
      </svg>
      <svg x="42" y="2" width="40" height="24" viewBox="0 0 10 10"
           preserveAspectRatio="xMidYMid slice">
        <rect width="10" height="10" fill="red"/>
      </svg>
      <svg x="98" y="2" width="36" height="24" viewBox="0 0 10 10"
           preserveAspectRatio="none">
        <rect width="10" height="10" fill="red"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    # meet: scale 2.4, content 24x24, centered vertically in 24x40
    # -> y from 10 to 34.
    assert_pixels(px, {
        (3, 10): RED,
        (25, 33): RED,
        (15, 9): WHITE,
        (15, 34): WHITE,
    })
    # slice: scale 4, content 40x40 centered in 40x24 viewport
    # -> viewport fully red, x 42..82, y 2..26.
    assert_pixels(px, {
        (43, 3): RED,
        (81, 25): RED,
        (41, 15): WHITE,
        (83, 15): WHITE,
        (60, 1): WHITE,
        (60, 26): WHITE,
    })
    # none: independent scales, content fills the whole 36x24 viewport.
    assert_pixels(px, {
        (99, 3): RED,
        (133, 25): RED,
        (97, 15): WHITE,
        (134, 15): WHITE,
        (115, 1): WHITE,
        (115, 26): WHITE,
    })


def test_nonzero_viewbox_origin_meet():
    """A viewBox with a non-zero min-x/min-y maps correctly, including clipping
    of overflowing content."""
    # Viewport (10,10) 40x60, viewBox -5 -5 10 10, meet: scale 4,
    # content 40x40 centered vertically -> user origin at device (10, 20).
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect width="120" height="120" fill="white"/>
      <svg x="10" y="10" width="40" height="60" viewBox="-5 -5 10 10"
           preserveAspectRatio="xMidYMid meet">
        <rect x="-5" y="-5" width="10" height="10" fill="red"/>
      </svg>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (11, 21): RED,
        (49, 59): RED,
        (30, 19): WHITE,   # letterbox above
        (30, 60): WHITE,   # letterbox below
        (9, 40): WHITE,
        (50, 40): WHITE,
    })


def test_use_preserve_aspect_ratio_per_reference():
    """Different preserveAspectRatio on two uses of one symbol never leak
    between references."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="140" height="60">
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
      </defs>
      <rect width="140" height="60" fill="white"/>
      <use href="#s" x="5" y="5" width="50" height="50"
           preserveAspectRatio="none"/>
      <use href="#s" x="65" y="5" width="30" height="50"/>
      <use href="#s" x="105" y="5" width="30" height="50"
           preserveAspectRatio="xMidYMid slice"/>
    </svg>'''
    _, px = pixels(svg)
    # First use, none: fills the whole 50x50 box.
    assert_pixels(px, {
        (6, 6): RED,
        (54, 54): RED,
    })
    # Second use, default meet in 30x50: scale 3, 30x30 centered at y=15..45.
    assert_pixels(px, {
        (66, 16): RED,
        (94, 44): RED,
        (80, 14): WHITE,
        (80, 45): WHITE,
    })
    # Third use, slice in 30x50: scale 5, viewport fully red.
    assert_pixels(px, {
        (106, 6): RED,
        (134, 54): RED,
    })


def test_use_of_group_unchanged():
    """Plain use of a group keeps the historical behavior: x/y translate the
    content, width/height have no viewport meaning."""
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
        (11, 11): RED,    # first group translated by (10,10)
        (29, 29): RED,
        (41, 21): RED,    # second group translated by (40,20), still 20x20
        (59, 39): RED,
        (60, 30): WHITE,  # width/height on use of g are ignored
    })


def test_deeply_nested_viewport_restore():
    """Exiting a nested svg restores transform, viewport size and clipping for
    following siblings at every nesting level."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <svg x="10" y="10" width="80" height="80" viewBox="0 0 80 80">
        <rect width="80" height="80" fill="#eeeeee"/>
        <svg x="10" y="10" width="40" height="40" viewBox="0 0 20 20">
          <rect x="-10" y="-10" width="40" height="40" fill="red"/>
          <rect width="20" height="20" fill="blue"/>
        </svg>
        <!-- 50% of the inner viewport (80) = 40 wide -->
        <rect x="10" y="55" width="50%" height="10" fill="green"/>
      </svg>
      <!-- 50% of the outer viewport (100) = 50 wide -->
      <rect x="0" y="92" width="50%" height="8" fill="black"/>
    </svg>'''
    _, px = pixels(svg)
    assert_pixels(px, {
        (21, 21): BLUE,    # innermost viewport (20,20)..(60,60)
        (19, 20): (238, 238, 238),  # clip of innermost svg
        (60, 20): (238, 238, 238),
        (21, 60): (238, 238, 238),
        (30, 66): GREEN,   # inner sibling: x 20..60, y 65..75
        (59, 70): GREEN,
        (60, 70): (238, 238, 238),
        (25, 95): (0, 0, 0),       # outer sibling: 50 wide
        (49, 95): (0, 0, 0),
        (50, 95): WHITE,
    })


@pytest.mark.parametrize('overflow,outside_color', (
    ('hidden', WHITE),
    ('visible', RED),
))
def test_nested_svg_overflow_attribute(overflow, outside_color):
    """overflow=hidden (explicit) and visible differ for out-of-viewport
    content."""
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
