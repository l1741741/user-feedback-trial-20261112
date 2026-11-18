"""
Pattern tiling test suite.

Pixel-level tests for the interaction of patternUnits, viewBox and
patternTransform: tile period and phase must stay stable when the same
pattern is reused on outlines of different sizes, with different
fill-opacity, with offsets, transforms, and degenerate tiles.

"""

import io
from pathlib import Path

import cairocffi as cairo

from . import parser, surface, svg2png

RED = (255, 0, 0, 255)
BLUE = (0, 0, 255, 255)
TRANSPARENT = (0, 0, 0, 0)

DEMO = Path(__file__).parent.parent / 'demo' / 'pattern-tiling.svg'


def render(svg):
    """Render ``svg`` and return a ``pixel(x, y) -> (r, g, b, a)`` getter."""
    png = svg2png(bytestring=svg.encode('utf-8'))
    image = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    data = bytes(image.get_data())

    def pixel(x, y):
        offset = y * image.get_stride() + x * 4
        b, g, r, a = (data[offset + i] for i in range(4))
        return r, g, b, a

    return pixel


def assert_color(pixel, x, y, expected, tolerance=3):
    """Assert that pixel (x, y) matches ``expected`` RGBA values."""
    actual = pixel(x, y)
    assert all(
        abs(a - e) <= tolerance for a, e in zip(actual, expected)), (
        f'pixel ({x}, {y}): expected {expected}, got {actual}')


def test_user_space_tile_independent_of_host():
    """userSpaceOnUse tiles keep their size and phase on any host."""
    pixel = render('''
        <svg xmlns="http://www.w3.org/2000/svg" width="60" height="30">
          <pattern id="p" patternUnits="userSpaceOnUse"
                   width="10" height="10">
            <rect x="0" y="0" width="4" height="10" fill="red"/>
            <rect x="4" y="0" width="2" height="10" fill="blue"/>
          </pattern>
          <rect x="0" y="0" width="25" height="30" fill="url(#p)"/>
          <rect x="33" y="7" width="27" height="23" fill="url(#p)"/>
        </svg>''')
    # First outline: red band, blue band, transparent padding, period 10
    assert_color(pixel, 1, 5, RED)
    assert_color(pixel, 5, 5, BLUE)
    assert_color(pixel, 8, 5, TRANSPARENT)
    assert_color(pixel, 11, 5, RED)
    # Second outline (offset by 33, 7): same period, phase anchored on the
    # user space origin, not on the outline
    assert_color(pixel, 33, 10, RED)
    assert_color(pixel, 35, 10, BLUE)
    assert_color(pixel, 37, 10, TRANSPARENT)
    assert_color(pixel, 40, 10, RED)


def test_user_space_percentage_tile_size():
    """Percentages in userSpaceOnUse tiles refer to the viewport."""
    pixel = render('''
        <svg xmlns="http://www.w3.org/2000/svg" width="40" height="10">
          <pattern id="p" patternUnits="userSpaceOnUse"
                   width="50%" height="10">
            <rect x="0" y="0" width="10" height="10" fill="red"/>
          </pattern>
          <rect x="0" y="0" width="40" height="10" fill="url(#p)"/>
        </svg>''')
    # 50% of a 40-wide viewport: 20-unit tiles
    assert_color(pixel, 5, 5, RED)
    assert_color(pixel, 15, 5, TRANSPARENT)
    assert_color(pixel, 25, 5, RED)


def test_object_bounding_box_follows_each_host():
    """objectBoundingBox tiles follow each outline's box, in order."""
    pixel = render('''
        <svg xmlns="http://www.w3.org/2000/svg" width="200" height="60">
          <pattern id="p" width="0.5" height="0.5"
                   patternContentUnits="objectBoundingBox">
            <rect x="0" y="0" width="0.2" height="0.2" fill="red"/>
          </pattern>
          <rect x="10" y="10" width="40" height="40" fill="url(#p)"/>
          <rect x="100" y="5" width="80" height="20" fill="url(#p)"/>
        </svg>''')
    # First outline: 20x20 tiles anchored on its (10, 10) box corner,
    # red square covering the first 0.2 fraction (8x8 units) of each tile
    assert_color(pixel, 12, 12, RED)
    assert_color(pixel, 25, 12, TRANSPARENT)
    assert_color(pixel, 32, 12, RED)
    # Second outline, used after the first one: 40x10 tiles anchored on
    # its own (100, 5) box corner. The first use must not have consumed
    # or overwritten the pattern's width/height.
    assert_color(pixel, 102, 6, RED)
    assert_color(pixel, 120, 6, TRANSPARENT)
    assert_color(pixel, 142, 6, RED)
    assert_color(pixel, 105, 12, TRANSPARENT)


def test_object_bounding_box_reuse_order():
    """Referencing a bounding-box pattern in any order gives same tiles."""
    svg = '''
        <svg xmlns="http://www.w3.org/2000/svg" width="200" height="30">
          <pattern id="p" width="0.25" height="0.5"
                   patternContentUnits="objectBoundingBox">
            <rect x="0" y="0" width="0.1" height="0.1" fill="red"/>
          </pattern>
          <rect x="{x0}" y="5" width="{w}" height="20" fill="url(#p)"/>
          <rect x="{x1}" y="5" width="{w2}" height="20" fill="url(#p)"/>
        </svg>'''
    # Small outline first, then large, and the other way around: the tile
    # size of each outline must only depend on its own bounding box.
    pixel_forward = render(svg.format(x0=10, w=40, x1=100, w2=80))
    pixel_backward = render(svg.format(x0=100, w=80, x1=10, w2=40))
    for x, y in ((12, 6), (22, 6), (102, 6), (122, 6), (142, 6)):
        assert pixel_forward(x, y) == pixel_backward(x, y)
    # Absolute check: the red square (0.1 fraction of each box) repeats
    # with the tile period of each outline, anchored on its box corner
    for x, y in ((12, 6), (22, 6), (102, 6), (122, 6), (142, 6)):
        assert_color(pixel_forward, x, y, RED)
    assert_color(pixel_forward, 16, 6, TRANSPARENT)
    assert_color(pixel_forward, 108, 6, TRANSPARENT)


def test_reuse_fill_opacity_does_not_accumulate():
    """fill-opacity must not accumulate across references of a pattern."""
    pixel = render('''
        <svg xmlns="http://www.w3.org/2000/svg" width="90" height="10">
          <pattern id="p" patternUnits="userSpaceOnUse"
                   width="10" height="10">
            <rect x="0" y="0" width="10" height="10" fill="red"/>
          </pattern>
          <rect x="0" y="0" width="30" height="10"
                fill="url(#p)" fill-opacity="0.5"/>
          <rect x="30" y="0" width="30" height="10" fill="url(#p)"/>
          <rect x="60" y="0" width="30" height="10"
                fill="url(#p)" fill-opacity="0.5"/>
        </svg>''')
    first = pixel(15, 5)
    opaque = pixel(45, 5)
    second = pixel(75, 5)
    # The full-opacity reference between the two half-opacity ones must be
    # fully opaque, and the last one must be exactly like the first one
    assert opaque[3] == 255
    assert 120 <= first[3] <= 135
    assert first == second


def test_pattern_transform_rotation_keeps_origin():
    """Rotating a pattern rotates the tile grid around a stable origin."""
    pixel = render('''
        <svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">
          <pattern id="p" patternUnits="userSpaceOnUse"
                   width="20" height="20" patternTransform="rotate(90)">
            <rect x="0" y="0" width="4" height="4" fill="red"/>
          </pattern>
          <rect x="0" y="0" width="40" height="40" fill="url(#p)"/>
        </svg>''')
    # rotate(90) maps the pattern square (0..4, 0..4) onto user space
    # (-4..0, 0..4): red squares land on x in 16..20 (mod 20), y in 0..4
    assert_color(pixel, 17, 1, RED)
    assert_color(pixel, 36, 21, RED)
    assert_color(pixel, 15, 1, TRANSPARENT)
    assert_color(pixel, 17, 5, TRANSPARENT)
    assert_color(pixel, 1, 1, TRANSPARENT)


def test_pattern_transform_translation_shifts_grid():
    """patternTransform translations shift the whole tile grid."""
    pixel = render('''
        <svg xmlns="http://www.w3.org/2000/svg" width="30" height="10">
          <pattern id="p" patternUnits="userSpaceOnUse"
                   width="10" height="10" patternTransform="translate(5 0)">
            <rect x="0" y="0" width="5" height="10" fill="red"/>
          </pattern>
          <rect x="0" y="0" width="30" height="10" fill="url(#p)"/>
        </svg>''')
    assert_color(pixel, 6, 1, RED)
    assert_color(pixel, 16, 1, RED)
    assert_color(pixel, 2, 1, TRANSPARENT)


def test_tile_offset_and_padding_tile_continuously():
    """Tiles with x/y offsets and transparent padding stay continuous."""
    pixel = render('''
        <svg xmlns="http://www.w3.org/2000/svg" width="40" height="30">
          <pattern id="p" patternUnits="userSpaceOnUse"
                   x="6" y="4" width="12" height="12">
            <rect x="6" y="4" width="5" height="12" fill="red"/>
            <rect x="13" y="4" width="2" height="12" fill="blue"/>
          </pattern>
          <rect x="0" y="0" width="40" height="30" fill="url(#p)"/>
        </svg>''')
    # The tile window starts at (6, 4): the red band covers user space
    # x in 6..11 (mod 12), blue x in 13..15 (mod 12), both continuously
    # from top to bottom since the content spans the full tile height
    assert_color(pixel, 7, 2, RED)
    assert_color(pixel, 19, 2, RED)
    assert_color(pixel, 8, 25, RED)
    assert_color(pixel, 14, 10, BLUE)
    assert_color(pixel, 26, 10, BLUE)
    assert_color(pixel, 3, 3, TRANSPARENT)
    assert_color(pixel, 12, 5, TRANSPARENT)
    assert_color(pixel, 16, 5, TRANSPARENT)


def test_viewbox_scales_content_not_tile():
    """viewBox scales the content into the tile, tile keeps its size."""
    pixel = render('''
        <svg xmlns="http://www.w3.org/2000/svg" width="70" height="20">
          <pattern id="p" patternUnits="userSpaceOnUse"
                   width="12" height="12" viewBox="0 0 4 4">
            <rect x="0" y="0" width="2" height="4" fill="red"/>
            <rect x="2" y="0" width="1" height="4" fill="blue"/>
          </pattern>
          <rect x="0" y="0" width="30" height="20" fill="url(#p)"/>
          <rect x="37" y="3" width="33" height="17" fill="url(#p)"/>
        </svg>''')
    # viewBox 4 units mapped on a 12-unit tile: red 0..6, blue 6..9,
    # transparent padding 9..12, same on both outlines
    assert_color(pixel, 2, 5, RED)
    assert_color(pixel, 7, 5, BLUE)
    assert_color(pixel, 10, 5, TRANSPARENT)
    assert_color(pixel, 14, 5, RED)
    assert_color(pixel, 39, 5, RED)
    assert_color(pixel, 44, 5, BLUE)


def test_degenerate_patterns_fall_back_without_leaking():
    """Invalid or empty tiles fall back and don't leak the last paint."""
    pixel = render('''
        <svg xmlns="http://www.w3.org/2000/svg" width="140" height="40">
          <pattern id="zero" patternUnits="userSpaceOnUse"
                   width="0" height="10" patternTransform="rotate(45)">
            <rect x="0" y="0" width="5" height="5" fill="red"/>
          </pattern>
          <pattern id="negative" patternUnits="userSpaceOnUse"
                   width="-5" height="10">
            <rect x="0" y="0" width="5" height="5" fill="red"/>
          </pattern>
          <pattern id="empty" patternUnits="userSpaceOnUse"
                   width="10" height="10"/>
          <pattern id="good" patternUnits="userSpaceOnUse"
                   width="10" height="10">
            <rect x="0" y="0" width="5" height="10" fill="red"/>
          </pattern>
          <rect x="0" y="0" width="20" height="20" fill="url(#zero) blue"/>
          <rect x="25" y="0" width="20" height="20"
                fill="url(#negative) blue"/>
          <rect x="50" y="0" width="20" height="20" fill="url(#empty) blue"/>
          <rect x="75" y="0" width="20" height="20"
                fill="url(#missing) blue"/>
          <rect x="100" y="0" width="20" height="20" fill="url(#good)"/>
          <rect x="0" y="25" width="20" height="15" fill="lime"/>
        </svg>''')
    # Degenerate and unknown patterns use the fallback color
    assert_color(pixel, 5, 5, BLUE)
    assert_color(pixel, 30, 5, BLUE)
    assert_color(pixel, 55, 5, BLUE)
    assert_color(pixel, 80, 5, BLUE)
    # The valid pattern referenced after them is not affected
    assert_color(pixel, 102, 5, RED)
    assert_color(pixel, 107, 5, TRANSPARENT)
    assert_color(pixel, 112, 5, RED)
    # The patternTransform of the invalid pattern didn't leak into the
    # context: the plain rectangle below is painted straight
    assert_color(pixel, 1, 26, (0, 255, 0, 255))
    assert_color(pixel, 18, 38, (0, 255, 0, 255))


def test_pattern_definition_not_mutated():
    """Rendering must not consume or rewrite the pattern definition."""
    svg = b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="20">
          <pattern id="p" width="0.5" height="0.5">
            <rect x="0" y="0" width="3" height="3" fill="red"/>
          </pattern>
          <rect x="0" y="0" width="40" height="20"
                fill="url(#p)" fill-opacity="0.5"/>
          <rect x="50" y="0" width="30" height="20" fill="url(#p)"/>
        </svg>'''
    tree = parser.Tree(bytestring=svg)
    surface.PNGSurface(tree, None, 96)
    node = surface.PNGSurface(tree, None, 96).patterns['p']
    # Attributes are the original ones: no consumed width/height, no
    # accumulated opacity, no leftover transform, tag untouched
    assert node['width'] == '0.5'
    assert node['height'] == '0.5'
    assert 'opacity' not in node
    assert 'transform' not in node
    assert node.tag == 'pattern'


def test_demo_file_period_phase_and_transparency():
    """The exported demo keeps period, phase and transparent regions."""
    pixel = render(DEMO.read_text())
    # First outline (16, 16, 240x120) uses a 16x16 user-space tile:
    # pixels repeat with a 16-unit period in both directions
    for x, y in ((20, 20), (40, 60), (100, 100)):
        assert pixel(x, y) == pixel(x + 16, y) == pixel(x, y + 16)
    # Second outline (300, 24, 173x88) shares the same user-space phase:
    # identical pixels at identical user-space coordinates
    for x, y in ((32, 30), (48, 40), (64, 80)):
        assert pixel(x, y) == pixel(x + 16, y)
    # Transparent padding shows the light background (250, 250, 250)
    background = (250, 250, 250, 255)
    assert_color(pixel, 16 + 15, 16 + 12, background)
    # Opacity row: the middle outline uses the pattern at full opacity,
    # the side ones at fill-opacity 0.4. The demo has an opaque
    # background, so compare colors at motif points (tile position (5, 5)
    # is inside the teal arrow): the middle one shows the pure motif
    # color, the side ones show the same blended color (no accumulation
    # across references).
    teal = (15, 118, 110, 255)
    assert_color(pixel, 201, 341, teal)
    left = pixel(21, 345)
    right = pixel(389, 345)
    assert left == right
    assert left != teal
