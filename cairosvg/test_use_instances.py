"""
Pixel-level regression tests for ``use`` reference instances.

The fixtures are small, font-free, pure-color SVGs. Checked pixels are
interior or exterior points chosen by analytic geometry, so they never lie on
an antialiased edge and exact RGBA comparisons are deterministic across Cairo
versions.

Colors:

    white  = (255, 255, 255)
    red    = (255, 0, 0)
    green  = (0, 128, 0)
    blue   = (0, 0, 255)
    navy   = (34, 68, 102)      # #224466
    crimson = (204, 51, 85)     # #cc3355
    yellow = (255, 204, 0)      # #ffcc00

"""

import io
from pathlib import Path

import pytest

from . import svg2png

THEMES = ((34, 68, 102), (204, 51, 85))
BADGE = (255, 204, 0)
LEGEND = (
    Path(__file__).parent.parent /
    'test_non_regression' / 'use_instances' / 'legend.svg')

WHITE = (255, 255, 255)
RED = (255, 0, 0)
GREEN = (0, 128, 0)
BLUE = (0, 0, 255)
NAVY = (34, 68, 102)
YELLOW = (255, 204, 0)


def pixels(svg_source):
    """Render ``svg_source`` and return a pixel accessor."""
    from PIL import Image
    png = svg2png(svg_source.encode() if isinstance(svg_source, str)
                  else svg_source)
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    return image.load()


def assert_pixels(accessor, expected):
    """Assert that each ``(x, y): color`` probe matches exactly."""
    for (x, y), color in expected.items():
        assert accessor[x, y][:3] == color, (
            f'pixel ({x}, {y}) = {accessor[x, y][:3]}, expected {color}')


def assert_same_image(svg_a, svg_b):
    """Assert that two SVG sources render to the exact same pixels."""
    from PIL import Image
    image_a = Image.open(io.BytesIO(svg2png(svg_a.encode()))).convert('RGBA')
    image_b = Image.open(io.BytesIO(svg2png(svg_b.encode()))).convert('RGBA')
    assert image_a.size == image_b.size
    pixels_a, pixels_b = image_a.load(), image_b.load()
    differences = [
        (x, y, pixels_a[x, y], pixels_b[x, y])
        for y in range(image_a.height) for x in range(image_a.width)
        if pixels_a[x, y] != pixels_b[x, y]]
    assert not differences, differences[:10]


def test_use_instances_keep_own_size_and_style():
    """Two references to one symbol use their own viewport and fill."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="140" height="60">
      <rect width="140" height="60" fill="white"/>
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10"/>
          <rect x="7" y="7" width="3" height="3" fill="blue"/>
        </symbol>
      </defs>
      <use href="#s" x="0" y="0" width="20" height="20" fill="green"/>
      <use href="#s" x="40" y="0" width="60" height="20" fill="red"/>
    </svg>'''
    accessor = pixels(svg)
    # First instance: scale 2, green square with its blue corner marker.
    assert_pixels(accessor, {
        (1, 1): GREEN,
        (15, 15): BLUE,
        (19, 1): GREEN,
        (20, 10): WHITE,
    })
    # Second instance: viewport 60x20 for a 1:1 viewBox, `meet` scale 2 and
    # horizontal centering, so content spans x 60..79. The blue marker is
    # stretched to a 6x6 square at the bottom right.
    assert_pixels(accessor, {
        (61, 1): RED,
        (75, 15): BLUE,
        (79, 17): BLUE,
        (59, 10): WHITE,
        (80, 10): WHITE,
    })


def test_use_x_y_combines_with_transform():
    """``x``/``y`` translate the instance, then its own transform applies."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect width="120" height="120" fill="white"/>
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
      </defs>
      <use href="#s" x="20" y="10" width="10" height="10"
           transform="translate(10,30)"/>
    </svg>'''
    accessor = pixels(svg)
    # The instance is at (20, 10) then translated by (10, 30): (30, 40).
    assert_pixels(accessor, {
        (31, 41): RED,
        (39, 49): RED,
        (29, 40): WHITE,
        (40, 40): WHITE,
        (35, 39): WHITE,
        (35, 50): WHITE,
    })


def test_use_scaled_transform_uses_instance_geometry():
    """A scale transform magnifies the positioned instance, not its size."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect width="120" height="120" fill="white"/>
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
      </defs>
      <use href="#s" x="10" y="10" width="20" height="20"
           transform="scale(2)"/>
    </svg>'''
    accessor = pixels(svg)
    # The 20x20 instance is scaled to 40x40 around the origin: (20, 20).
    assert_pixels(accessor, {
        (21, 21): RED,
        (59, 59): RED,
        (19, 20): WHITE,
        (60, 20): WHITE,
        (30, 19): WHITE,
        (30, 60): WHITE,
    })


def test_symbol_single_dimension_width():
    """Only ``width`` on a symbol instance is a valid, independent override."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect width="120" height="120" fill="white"/>
      <defs>
        <symbol id="s" viewBox="0 0 10 20">
          <rect width="10" height="20" fill="red"/>
        </symbol>
      </defs>
      <use href="#s" x="0" y="0" width="40"/>
    </svg>'''
    accessor = pixels(svg)
    # Viewport is 40 wide and, with no height, 100% of the parent (120) tall.
    # `meet` scales by 4, so content is 40x80, centered vertically at y 20..99.
    assert_pixels(accessor, {
        (1, 21): RED,
        (38, 98): RED,
        (1, 19): WHITE,
        (1, 100): WHITE,
    })


def test_symbol_single_dimension_height():
    """Only ``height`` on a symbol instance is a valid independent override."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">
      <rect width="120" height="120" fill="white"/>
      <defs>
        <symbol id="s" viewBox="0 0 10 20">
          <rect width="10" height="20" fill="red"/>
        </symbol>
      </defs>
      <use href="#s" x="50" y="0" height="60"/>
    </svg>'''
    accessor = pixels(svg)
    # Viewport is 100% of the parent (120) wide and 60 tall. `meet` scales by
    # 3, so content is 30x60, centered horizontally at x 95..124 (clipped).
    assert_pixels(accessor, {
        (96, 1): RED,
        (118, 58): RED,
        (94, 30): WHITE,
        (95, 61): WHITE,
    })


def test_symbol_single_dimension_each_way_independent():
    """The two references differ only by the specified dimension."""
    symbol = (
        '<symbol id="s" viewBox="0 0 10 20">'
        '<rect width="10" height="20" fill="red"/></symbol>')
    svg_width = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <defs>{symbol}</defs>
      <use href="#s" x="0" y="0" width="40"/>
    </svg>'''
    svg_height = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
      <rect width="100" height="100" fill="white"/>
      <defs>{symbol}</defs>
      <use href="#s" x="0" y="0" height="40"/>
    </svg>'''
    accessor = pixels(svg_width)
    # Width 40, height 100% => meet scale 4 => content 40x80 at y 10..89.
    assert_pixels(accessor, {(1, 11): RED, (38, 88): RED, (20, 9): WHITE})
    accessor = pixels(svg_height)
    # Height 40, width 100% => meet scale 2 => content 20x40 at x 40..59.
    assert_pixels(accessor, {(41, 1): RED, (58, 38): RED, (39, 20): WHITE})


def test_use_of_svg_element_keeps_its_own_geometry():
    """A referenced ``svg`` keeps its intrinsic size unless overridden."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">
      <rect width="120" height="80" fill="white"/>
      <defs>
        <svg id="v" x="5" y="7" width="20" height="20" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </svg>
      </defs>
      <use href="#v" x="10" y="10"/>
      <use href="#v" x="50" y="10" width="40"/>
      <use href="#v" x="50" y="40" height="30"/>
    </svg>'''
    accessor = pixels(svg)
    # No override: the referenced svg paints at its own (x, y) and size:
    # content at (15, 17)..(34, 36).
    assert_pixels(accessor, {
        (16, 18): RED,
        (33, 35): RED,
        (14, 20): WHITE,
        (35, 25): WHITE,
    })
    # Width only: width 40, intrinsic height 20, meet scale 2 -> content is
    # 20x20, centered horizontally in 40 -> x 65..84, y 17..36.
    assert_pixels(accessor, {
        (66, 18): RED,
        (83, 35): RED,
        (64, 20): WHITE,
        (85, 20): WHITE,
    })
    # Height only: intrinsic width 20, height 30, meet scale 2 -> content is
    # 20x20, centered vertically in 30 -> x 55..74, y 52..71.
    assert_pixels(accessor, {
        (56, 53): RED,
        (73, 70): RED,
        (54, 55): WHITE,
        (75, 55): WHITE,
    })


def test_explicit_content_attributes_win_over_inherited():
    """A fill set on the referenced content is not overridden by the use."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">
      <rect width="120" height="60" fill="white"/>
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="5" height="10" fill="blue"/>
          <rect x="5" y="0" width="5" height="10"/>
        </symbol>
      </defs>
      <use href="#s" x="0" y="0" width="40" height="40" fill="green"/>
    </svg>'''
    accessor = pixels(svg)
    # Left half: explicit blue on the content wins over the instance fill.
    # Right half: no explicit fill, so the inherited green applies.
    assert_pixels(accessor, {
        (1, 1): BLUE,
        (38, 1): GREEN,
        (1, 38): BLUE,
        (38, 38): GREEN,
    })


def test_instance_style_wins_over_instance_presentation_attribute():
    """A ``style`` on the instance wins over its presentation attribute."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">
      <rect width="120" height="40" fill="white"/>
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10"/>
        </symbol>
      </defs>
      <use href="#s" x="0" y="0" width="40" height="40" fill="green"/>
      <use href="#s" x="60" y="0" width="40" height="40"
           fill="green" style="fill:blue"/>
    </svg>'''
    accessor = pixels(svg)
    # The first instance uses its presentation attribute, the second one its
    # style declaration. The shared definition must not leak either value.
    assert_pixels(accessor, {(1, 1): GREEN, (61, 1): BLUE})


def test_nested_use_composes_button_from_symbols():
    """Nested references compose a button and scale with each instance."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="160" height="70">
      <rect width="160" height="70" fill="white"/>
      <defs>
        <symbol id="bg" viewBox="0 0 20 10">
          <rect width="20" height="10" fill="#224466"/>
        </symbol>
        <symbol id="icon" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="#ffcc00"/>
        </symbol>
        <symbol id="button" viewBox="0 0 20 10">
          <use href="#bg" width="20" height="10"/>
          <use href="#icon" x="5" y="1" width="8" height="8"/>
        </symbol>
      </defs>
      <use href="#button" x="5" y="5" width="80" height="40"/>
      <use href="#button" x="95" y="5" width="60" height="30"/>
    </svg>'''
    accessor = pixels(svg)
    # First button: scale 4. Background navy, icon yellow centered.
    assert_pixels(accessor, {
        (6, 6): NAVY,
        (45, 25): YELLOW,
        (84, 44): NAVY,
        (85, 25): WHITE,
    })
    # Second button: scale 3. Same composition, own position and size.
    assert_pixels(accessor, {
        (96, 6): NAVY,
        (125, 20): YELLOW,
        (154, 34): NAVY,
        (155, 20): WHITE,
    })


def test_nested_use_keeps_inner_position_per_instance():
    """A referenced definition containing ``use`` is not rewritten."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="60">
      <rect width="120" height="60" fill="white"/>
      <defs>
        <symbol id="icon" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
        <g id="pair">
          <use href="#icon" x="10" y="5" width="10" height="10"/>
        </g>
      </defs>
      <use href="#pair" x="0" y="0"/>
      <use href="#pair" x="50" y="0"/>
    </svg>'''
    accessor = pixels(svg)
    # Both instances keep the inner use at its own position ((10, 5) offsets).
    assert_pixels(accessor, {
        (11, 6): RED,
        (19, 14): RED,
        (61, 6): RED,
        (69, 14): RED,
        (9, 10): WHITE,
        (20, 10): WHITE,
    })


def test_painting_order_is_invariant():
    """Swapping the drawing order of instances changes nothing per instance."""
    def document(order):
        instances = (
            ('<use href="#s" x="0" y="0" width="30" height="30" '
             'fill="green"/>'),
            ('<use href="#s" x="40" y="0" width="60" height="20" '
             'fill="red"/>'),
        )
        return f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">
          <rect width="120" height="40" fill="white"/>
          <defs>
            <symbol id="s" viewBox="0 0 10 10">
              <rect width="10" height="10"/>
              <rect x="7" y="7" width="3" height="3" fill="blue"/>
            </symbol>
          </defs>
          {''.join(instances[index] for index in order)}
        </svg>'''

    assert_same_image(document((0, 1)), document((1, 0)))


def test_overlapping_instances_paint_in_document_order():
    """Instances that overlap follow the document order, not caching order."""
    def document(order):
        instances = (
            '<use href="#s" x="0" y="0" width="40" height="40" fill="red"/>',
            ('<use href="#s" x="20" y="0" width="40" height="40" '
             'fill="green"/>'),
        )
        return f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="70" height="40">
          <rect width="70" height="40" fill="white"/>
          <defs>
            <symbol id="s" viewBox="0 0 10 10">
              <rect width="10" height="10"/>
            </symbol>
          </defs>
          {''.join(instances[index] for index in order)}
        </svg>'''

    accessor = pixels(document((0, 1)))
    # Green (drawn second) covers the overlap: x 20..59.
    assert_pixels(accessor, {
        (1, 20): RED,
        (19, 20): RED,
        (21, 20): GREEN,
        (41, 20): GREEN,
        (59, 20): GREEN,
        (60, 20): WHITE,
    })
    accessor = pixels(document((1, 0)))
    # Red (now drawn second) covers the overlap: x 0..39.
    assert_pixels(accessor, {
        (1, 20): RED,
        (39, 20): RED,
        (41, 20): GREEN,
        (59, 20): GREEN,
        (60, 20): WHITE,
    })


def test_shared_pattern_definition_not_rewritten_by_instances():
    """A pattern reused by two differently sized shapes keeps both."""
    def document(order):
        instances = (
            ('<rect x="0" y="10" width="20" height="20" '
             'fill="url(#p)"/>'),
            ('<rect x="40" y="10" width="60" height="40" '
             'fill="url(#p)"/>'),
        )
        return f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="120" height="70">
          <rect width="120" height="70" fill="white"/>
          <defs>
            <pattern id="p" width="0.5" height="0.5"
                     patternContentUnits="userSpaceOnUse">
              <rect width="10" height="10" fill="red"/>
            </pattern>
          </defs>
          {''.join(instances[index] for index in order)}
        </svg>'''

    expected = {
        # First shape: 10-unit tile at scale 1, so the whole 0..19 tile is red.
        (1, 15): RED,
        (19, 15): RED,
        (21, 15): WHITE,
        # Second shape: the same pattern scaled by its 60x40 bounding box,
        # so the tile becomes 30x20 with red squares of 30x20 user units.
        (61, 25): RED,
        (71, 25): WHITE,
        (91, 25): RED,
    }
    assert_pixels(pixels(document((0, 1))), expected)
    assert_pixels(pixels(document((1, 0))), expected)
    assert_same_image(document((0, 1)), document((1, 0)))


def test_shared_mask_definition_not_rewritten_by_instances():
    """A mask reused by two instances keeps each instance’s geometry."""
    def document(order):
        instances = (
            ('<rect x="0" y="10" width="40" height="30" fill="red" '
             'mask="url(#m)"/>'),
            ('<rect x="50" y="10" width="20" height="20" fill="red" '
             'mask="url(#m)"/>'),
        )
        return f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="90" height="60">
          <rect width="90" height="60" fill="white"/>
          <defs>
            <mask id="m" maskUnits="userSpaceOnUse"
                  x="0" y="0" width="100" height="100"
                  maskContentUnits="userSpaceOnUse">
              <rect x="0" y="0" width="30" height="100" fill="white"/>
            </mask>
          </defs>
          {''.join(instances[index] for index in order)}
        </svg>'''

    expected = {
        # User-space mask: only x 0..29 is painted, for both instances.
        (1, 20): RED,
        (29, 20): RED,
        (35, 20): WHITE,
        (51, 20): WHITE,
    }
    assert_pixels(pixels(document((0, 1))), expected)
    assert_pixels(pixels(document((1, 0))), expected)
    assert_same_image(document((0, 1)), document((1, 0)))


def test_def_href_resolution_is_order_independent():
    """Resolving an href-based definition twice gives the same gradient."""
    def document(order):
        rectangles = (
            '<rect x="0" y="0" width="30" height="30" fill="url(#g1)"/>',
            '<rect x="50" y="0" width="50" height="30" fill="url(#g1)"/>',
        )
        return f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">
          <rect width="120" height="40" fill="white"/>
          <defs>
            <linearGradient id="g2">
              <stop offset="0" stop-color="red"/>
              <stop offset="1" stop-color="blue"/>
            </linearGradient>
            <linearGradient id="g1" href="#g2"
                            x1="0" y1="0" x2="1" y2="0"/>
          </defs>
          {''.join(rectangles[index] for index in order)}
        </svg>'''

    assert_same_image(document((0, 1)), document((1, 0)))


def test_use_of_missing_reference_does_not_poison_later_instances():
    """A missing reference is skipped; following valid instances still draw."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">
      <rect width="120" height="40" fill="white"/>
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
      </defs>
      <use href="#does-not-exist" x="0" y="0" width="20" height="20"/>
      <use href="#s" x="40" y="0" width="20" height="20"/>
    </svg>'''
    accessor = pixels(svg)
    assert_pixels(accessor, {
        (1, 1): WHITE,
        (41, 1): RED,
        (59, 19): RED,
    })


def test_use_without_href_is_ignored():
    """A ``use`` without an ``href`` references nothing and draws nothing."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">
      <rect width="120" height="40" fill="white"/>
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
      </defs>
      <use x="0" y="0" width="20" height="20"/>
      <use href="#s" x="40" y="0" width="20" height="20"/>
    </svg>'''
    accessor = pixels(svg)
    assert_pixels(accessor, {(1, 1): WHITE, (41, 1): RED})


@pytest.mark.parametrize('definitions', (
    # Self-reference.
    '<use id="a" href="#a" width="10" height="10"/>',
    # Two-element cycle.
    ('<use id="a" href="#b" width="10" height="10"/>'
     '<use id="b" href="#a" width="10" height="10"/>'),
    # Longer cycle.
    ('<use id="a" href="#b"/>'
     '<use id="b" href="#c"/>'
     '<use id="c" href="#a"/>'),
))
def test_reference_cycles_are_skipped(definitions):
    """A cycle is aborted like an unresolvable reference, not rendered."""
    svg = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="40">
      <rect width="120" height="40" fill="white"/>
      <defs>
        {definitions}
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
      </defs>
      <use href="#s" x="0" y="0" width="20" height="20"/>
      <use href="#a" x="40" y="0" width="20" height="20"/>
      <use href="#s" x="80" y="0" width="20" height="20"/>
    </svg>'''
    accessor = pixels(svg)
    # The two legitimate instances around the cycle are drawn as usual.
    assert_pixels(accessor, {
        (1, 1): RED,
        (41, 1): WHITE,
        (81, 1): RED,
    })


def test_many_instances_do_not_exhaust_reference_budget():
    """Repeated valid instances are not mistaken for a too-deep document."""
    uses = ''.join(
        f'<use href="#s" x="{index * 2}" y="0" width="2" height="2"/>'
        for index in range(64))
    svg = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="130" height="10">
      <rect width="130" height="10" fill="white"/>
      <defs>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
      </defs>
      {uses}
    </svg>'''
    accessor = pixels(svg)
    assert_pixels(accessor, {
        (1, 1): RED,
        (63, 1): RED,
        (127, 1): RED,
    })


def test_legend_fixture_exports_instance_layout(tmp_path):
    """The legend fixture exports a >=240x160 PNG with both themes and badges.

    The fixture is a real-world icon-library legend: each instance has its own
    position, scale and theme color, and every icon carries a visible corner
    badge so that the instance bounds can be checked on the exported pixels.
    """
    from PIL import Image
    output = tmp_path / 'legend.png'
    svg2png(url=LEGEND, write_to=str(output))
    image = Image.open(output).convert('RGB')
    assert image.width >= 240
    assert image.height >= 160

    accessor = image.load()
    # Row 1, left-to-right: 24px navy, 40px crimson, 64x32 navy icon, then a
    # translated and scaled crimson icon.
    assert_pixels(accessor, {
        (20, 24): THEMES[0],
        (36, 24): THEMES[0],
        (66, 20): THEMES[1],
        (86, 20): THEMES[1],
        (144, 20): THEMES[0],
        (150, 20): THEMES[0],
        (210, 10): THEMES[1],
        (226, 28): THEMES[1],
    })
    # Corner badges are 5x5 user units, scaled by the instance.
    assert_pixels(accessor, {
        (36, 24): THEMES[0],
        (35, 35): BADGE,
        (90, 50): BADGE,
        (165, 45): BADGE,
        (230, 30): BADGE,
    })
    # Row 2: single-dimension instances, each in its own nested viewport.
    assert_pixels(accessor, {
        (22, 74): THEMES[1],
        (42, 74): THEMES[1],
        (110, 74): THEMES[0],
        (114, 74): THEMES[0],
    })
    # Row 3: composed buttons, both themes, with the inner icon and its badge.
    assert_pixels(accessor, {
        (104, 140): THEMES[0],
        (110, 140): THEMES[0],
        (196, 140): THEMES[1],
        (200, 140): THEMES[1],
        (236, 164): THEMES[1],
    })


def test_legend_instance_order_is_invariant(tmp_path):
    """Painting the legend’s instances in another order changes nothing."""
    from PIL import Image
    source = LEGEND.read_text()

    # Move the composed-button references before the icon rows: the definition
    # resolution and the instance state must be independent from paint order.
    row_3_start = source.index('<!-- Row 3:')
    row_3_end = source.rindex('</svg>')
    row_3 = source[row_3_start:row_3_end]
    reordered = source[:row_3_start] + source[row_3_end:]
    insertion = reordered.index('<!-- Row 1:')
    reordered = (
        reordered[:insertion] + row_3 + '\n  ' + reordered[insertion:])

    def render(text):
        png = svg2png(text.encode())
        return Image.open(io.BytesIO(png)).convert('RGBA')

    image_a = render(source)
    image_b = render(reordered)
    assert image_a.size == image_b.size
    pixels_a, pixels_b = image_a.load(), image_b.load()
    differences = [
        (x, y, pixels_a[x, y], pixels_b[x, y])
        for y in range(image_a.height) for x in range(image_a.width)
        if pixels_a[x, y] != pixels_b[x, y]]
    assert not differences, differences[:10]


def test_cycle_inside_object_bounding_box_gradient_is_skipped():
    """A cycle reached while computing a bounding box is aborted too."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="40">
      <rect width="80" height="40" fill="white"/>
      <defs>
        <linearGradient id="g">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
        <use id="a" href="#a" width="10" height="10"/>
        <symbol id="s" viewBox="0 0 10 10">
          <rect width="10" height="10" fill="red"/>
        </symbol>
      </defs>
      <use href="#a" x="0" y="0" fill="url(#g)"/>
      <use href="#s" x="40" y="0" width="20" height="20"/>
    </svg>'''
    accessor = pixels(svg)
    assert_pixels(accessor, {
        (1, 1): WHITE,
        (41, 1): RED,
        (59, 19): RED,
    })


def test_deep_acyclic_chain_is_rendered():
    """A long but acyclic chain of references is valid and fully rendered."""
    depth = 40
    chain = ''.join(
        f'<g id="u{index}"><use href="#u{index + 1}" x="1" y="0"/></g>'
        for index in range(depth))
    svg = f'''
    <svg xmlns="http://www.w3.org/2000/svg" width="80" height="20">
      <rect width="80" height="20" fill="white"/>
      <defs>
        {chain}
        <g id="u{depth}"><rect width="10" height="10" fill="red"/></g>
      </defs>
      <use href="#u0" x="5" y="5"/>
    </svg>'''
    accessor = pixels(svg)
    # Each level adds a 1-unit offset, so the icon is at (5 + 40, 5).
    assert_pixels(accessor, {
        (46, 6): RED,
        (54, 14): RED,
        (44, 10): WHITE,
        (55, 10): WHITE,
    })
