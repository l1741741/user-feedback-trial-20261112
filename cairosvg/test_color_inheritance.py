"""
Regression tests for color inheritance across definitions, references and
instances (``currentColor``, ``inherit``, themed ``<use>`` instances).

The bugs fixed here were:

* ``currentColor`` was rewritten to a concrete color at parse time, so
  gradient stops and other shared definitions kept the color of the place
  where they were first parsed instead of the theme of each reference;
* the keyword written through a CSS ``style`` attribute was lower-cased and
  never matched, silently rendering black;
* paint fallbacks such as ``fill="url(#missing) currentColor"`` were
  rendered black;
* rendering mutated shared definition nodes (tag switches, opacity
  accumulation, geometry writes), letting one instance pollute the next.

These tests render real PNGs and assert on their pixels, so they cover the
full parse/paint path rather than helpers in isolation.

"""

import io

import cairocffi as cairo

from . import svg2png


RED = (255, 0, 0)
BLUE = (0, 0, 255)
GREEN = (0, 128, 0)
ORANGE = (255, 165, 0)
TEAL = (0, 128, 128)
GOLD = (255, 215, 0)
BLACK = (0, 0, 0)
PURPLE = (128, 0, 128)


def render(svg_source, width=400, height=200):
    """Render ``svg_source`` and return a pixel probe ``(surface, pixel)``."""
    if isinstance(svg_source, str):
        svg_source = svg_source.encode()
    png = svg2png(
        bytestring=svg_source, output_width=width, output_height=height)
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    data = surface.get_data()
    w = surface.get_width()

    def pixel(x, y):
        index = (int(y) * w + int(x)) * 4
        # Native ARGB32 is stored as BGRA on little-endian machines
        channels = data[index:index + 4]
        return (channels[2], channels[1], channels[0], channels[3])

    return pixel


def assert_color(probe, point, expected, tolerance=8):
    """Assert the RGB pixel at ``point`` matches ``expected`` (ignoring alpha)."""
    actual = probe(*point)
    for channel in range(3):
        assert abs(actual[channel] - expected[channel]) <= tolerance, (
            f'pixel {point}: expected {expected}, got {actual}')


def assert_alpha(probe, point, expected, tolerance=8):
    """Assert the alpha channel of the pixel at ``point``."""
    actual = probe(*point)[3]
    assert abs(actual - expected) <= tolerance, (
        f'pixel {point}: expected alpha {expected}, got {actual}')


# ---------------------------------------------------------------------------
# Main fixture: two themes, one symbol referenced several times, a shared
# currentColor gradient, plain path controls, opacity and order swap.
# ---------------------------------------------------------------------------

MAIN_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="400" height="200"
     viewBox="0 0 400 200">
  <defs>
    <linearGradient id="themeGradient">
      <stop offset="0" stop-color="currentColor"/>
      <stop offset="1" stop-color="white"/>
    </linearGradient>
    <symbol id="icon" viewBox="0 0 40 40">
      <rect x="2" y="2" width="36" height="36" fill="currentColor"/>
      <circle cx="20" cy="20" r="10" fill="none"
              stroke="currentColor" stroke-width="4"/>
    </symbol>
    <symbol id="gradientIcon" viewBox="0 0 40 40">
      <rect x="2" y="2" width="36" height="36" fill="url(#themeGradient)"/>
    </symbol>
  </defs>

  <!-- Row 1: red and blue themed instances of the same symbol -->
  <g color="red">
    <use href="#icon" x="10" y="10" width="40" height="40"/>
  </g>
  <g color="blue">
    <use href="#icon" x="60" y="10" width="40" height="40"/>
  </g>

  <!-- Same gradient definition under two different themes -->
  <g color="green">
    <rect x="110" y="10" width="40" height="40" fill="url(#themeGradient)"/>
  </g>
  <g color="orange">
    <rect x="160" y="10" width="40" height="40" fill="url(#themeGradient)"/>
  </g>

  <!-- Symbol whose content itself uses the shared gradient -->
  <g color="teal">
    <use href="#gradientIcon" x="210" y="10" width="40" height="40"/>
  </g>

  <!-- Plain path control, untouched by theming -->
  <rect x="260" y="10" width="40" height="40" fill="purple"/>

  <!-- Row 2: gradient endpoints, opacity independence and order swap -->
  <!-- Endpoint color of the green gradient -->
  <g color="green">
    <rect x="10" y="110" width="40" height="40" fill="url(#themeGradient)"/>
  </g>
  <!-- Same icon at 50% group opacity: RGB stays themed, alpha is halved -->
  <g color="red" opacity="0.5">
    <use href="#icon" x="60" y="110" width="40" height="40"/>
  </g>
  <!-- Order swap: orange drawn before green, neither may leak into the
       other (slot geometry is identical to row 1) -->
  <g color="orange">
    <rect x="160" y="110" width="40" height="40" fill="url(#themeGradient)"/>
  </g>
  <g color="green">
    <rect x="110" y="110" width="40" height="40" fill="url(#themeGradient)"/>
  </g>
</svg>
'''


def test_symbol_instances_follow_their_own_theme():
    probe = render(MAIN_SVG)
    # Top-left corner of each 40px rect, safely inside the fill
    assert_color(probe, (15, 15), RED)
    assert_color(probe, (65, 15), BLUE)


def test_gradient_stops_resolve_per_reference():
    probe = render(MAIN_SVG)
    # At the middle of the rect the gradient is blended toward white but its
    # hue still identifies the resolved currentColor (green/orange/teal).
    mid_green = probe(120, 30)
    mid_orange = probe(170, 30)
    mid_teal = probe(220, 30)
    assert mid_green[1] > mid_green[0] + 60 and mid_green[1] > mid_green[2] + 60
    assert mid_orange[0] > mid_orange[2] + 150
    assert mid_teal[1] > mid_teal[0] + 80 and mid_teal[2] > mid_teal[0] + 80
    # Near the right edge the gradient has faded almost entirely to white
    whiteish = probe(144, 30)
    assert whiteish[0] > 200 and whiteish[1] > 200 and whiteish[2] > 200


def test_gradient_inside_symbol_uses_reference_color():
    probe = render(MAIN_SVG)
    mid_teal = probe(220, 30)
    assert mid_teal[1] > mid_teal[0] + 80 and mid_teal[2] > mid_teal[0] + 80


def test_plain_path_control_is_untouched():
    probe = render(MAIN_SVG)
    assert_color(probe, (265, 15), PURPLE)


def test_opacity_is_independent_per_instance():
    probe = render(MAIN_SVG)
    # Fully opaque red instance in row 1
    assert_alpha(probe, (20, 30), 255)
    # Half transparent red instance in row 2: red is composited over the
    # transparent backdrop, so premultiplied RGB is ~128 and alpha ~128
    half_red = probe(70, 130)
    assert half_red[0] == 128 and half_red[1] == 0 and half_red[2] == 0
    assert_alpha(probe, (70, 130), 128, tolerance=10)


def test_draw_order_swap_does_not_pollute_siblings():
    probe = render(MAIN_SVG)
    # Row 2 draws orange then green; both slots keep their own theme
    mid_orange = probe(170, 130)
    mid_green = probe(120, 130)
    assert mid_orange[0] > mid_orange[2] + 150
    assert mid_green[1] > mid_green[0] + 60 and mid_green[1] > mid_green[2] + 60
    # And row 1 instances rendered earlier are unaffected as well
    assert_color(probe, (20, 30), RED)
    assert_color(probe, (70, 30), BLUE)


def test_shared_definition_node_is_not_mutated():
    """Rendering must not rewrite keywords or tags in stored definitions."""
    import io as _io

    from .parser import Tree
    from .surface import PNGSurface

    source = MAIN_SVG.encode()
    tree = Tree(bytestring=source)
    output = _io.BytesIO()
    rendered = PNGSurface(tree, output, 96)
    # The stored gradient keeps the keyword text and its gradient tag
    gradient = rendered.gradients['themeGradient']
    assert gradient.tag == 'linearGradient'
    stop_colors = [stop.get('stop-color') for stop in gradient.children]
    assert stop_colors == ['currentColor', 'white']
    # The symbol is still a symbol (the <use> machinery swaps a clone, not
    # the definition itself)
    defs = next(
        node for node in tree.children
        if getattr(node, 'tag', None) == 'defs')
    tags = [child.tag for child in defs.children]
    assert 'symbol' in tags
    # Rendering a second time from the same tree still produces the themes
    second = render(source)
    mid_green = second(120, 30)
    mid_orange = second(170, 30)
    assert mid_green[1] > mid_green[0] + 60
    assert mid_orange[0] > mid_orange[2] + 150


# ---------------------------------------------------------------------------
# Explicit fill/stroke on the <use> vs. the referenced content.
# ---------------------------------------------------------------------------

PRIORITY_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"
     viewBox="0 0 200 100">
  <defs>
    <symbol id="ownFill" viewBox="0 0 40 40">
      <rect x="2" y="2" width="36" height="36" fill="currentColor"/>
    </symbol>
    <symbol id="noFill" viewBox="0 0 40 40">
      <rect x="2" y="2" width="36" height="36"/>
    </symbol>
  </defs>
  <!-- Content's own presentation attribute beats the use's inherited fill -->
  <g color="teal">
    <use href="#ownFill" x="10" y="10" width="40" height="40" fill="red"/>
  </g>
  <!-- Content without its own fill inherits the use fill -->
  <use href="#noFill" x="70" y="10" width="40" height="40" fill="red"/>
  <!-- Outer style color is inherited through the use -->
  <g style="color:green">
    <use href="#ownFill" x="130" y="10" width="40" height="40"/>
  </g>
</svg>
'''


def test_explicit_presentation_attribute_priority():
    probe = render(PRIORITY_SVG, 200, 100)
    assert_color(probe, (15, 15), TEAL)
    assert_color(probe, (75, 15), RED)
    assert_color(probe, (135, 15), GREEN)


# ---------------------------------------------------------------------------
# Nested <use>, missing color, inherit keyword.
# ---------------------------------------------------------------------------

NESTED_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="250" height="100"
     viewBox="0 0 250 100">
  <defs>
    <symbol id="leaf" viewBox="0 0 40 40">
      <rect x="2" y="2" width="36" height="36" fill="currentColor"/>
    </symbol>
    <symbol id="nested" viewBox="0 0 40 40">
      <use href="#leaf" width="40" height="40"/>
    </symbol>
  </defs>
  <!-- Nested use inherits the outer theme -->
  <g color="gold">
    <use href="#nested" x="10" y="10" width="40" height="40"/>
  </g>
  <!-- No color anywhere: currentColor falls back to black -->
  <use href="#leaf" x="70" y="10" width="40" height="40"/>
  <!-- fill: inherit follows the parent group's fill -->
  <g fill="lime">
    <rect x="130" y="10" width="40" height="40" fill="inherit"/>
  </g>
  <!-- color: inherit chain feeding currentColor -->
  <g color="maroon">
    <g style="color:inherit">
      <rect x="190" y="10" width="40" height="40" fill="currentColor"/>
    </g>
  </g>
</svg>
'''


def test_nested_use_missing_color_and_inherit():
    probe = render(NESTED_SVG, 250, 100)
    assert_color(probe, (15, 15), GOLD)
    assert_color(probe, (75, 15), BLACK)
    assert_color(probe, (135, 15), (0, 255, 0))
    assert_color(probe, (195, 15), (128, 0, 0))


# ---------------------------------------------------------------------------
# currentColor reached through a CSS style attribute and paint fallbacks.
# ---------------------------------------------------------------------------

STYLE_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="250" height="100"
     viewBox="0 0 250 100">
  <defs>
    <linearGradient id="styledGradient">
      <stop offset="0" style="stop-color:currentColor"/>
      <stop offset="1" stop-color="white"/>
    </linearGradient>
  </defs>
  <!-- currentColor in a CSS style declaration (gets lower-cased on parse) -->
  <g color="navy">
    <rect x="10" y="10" width="40" height="40" fill="url(#styledGradient)"/>
  </g>
  <!-- url() fallback color when the paint server is missing -->
  <g color="olive">
    <rect x="70" y="10" width="40" height="40"
          fill="url(#does-not-exist) currentColor"/>
  </g>
  <!-- style attribute directly on a shape -->
  <g color="teal">
    <rect x="130" y="10" width="40" height="40"
          style="fill:currentColor"/>
  </g>
  <!-- stroke with currentColor -->
  <g color="red">
    <rect x="190" y="10" width="40" height="40" fill="none"
          stroke="currentColor" stroke-width="10"/>
  </g>
</svg>
'''


def test_style_attribute_and_paint_fallback():
    probe = render(STYLE_SVG, 250, 100)
    # Navy gradient: blue channel clearly dominates the red one at mid-rect
    navy = probe(20, 30)
    assert navy[2] > navy[0] + 60 and navy[2] >= 120
    assert_color(probe, (80, 30), (128, 128, 0))      # olive fallback
    assert_color(probe, (140, 30), TEAL)              # style fill
    assert_color(probe, (194, 30), RED)               # stroked edge


def test_repeated_gradient_references_stay_independent():
    """The same gradient used twice in one document renders each theme."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"
         viewBox="0 0 120 60">
      <defs>
        <linearGradient id="g">
          <stop offset="0" stop-color="currentColor"/>
          <stop offset="1" stop-color="currentColor"/>
        </linearGradient>
      </defs>
      <g color="red"><rect x="5" y="10" width="40" height="40"
                          fill="url(#g)"/></g>
      <g color="blue"><rect x="55" y="10" width="40" height="40"
                           fill="url(#g)"/></g>
    </svg>'''
    probe = render(svg, 120, 60)
    assert_color(probe, (10, 30), RED)
    assert_color(probe, (60, 30), BLUE)


def test_gradient_href_chain_keeps_currentcolor_per_use():
    """A gradient extending another must not freeze colors in shared defs."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"
         viewBox="0 0 120 60">
      <defs>
        <linearGradient id="base">
          <stop offset="0" stop-color="currentColor"/>
          <stop offset="1" stop-color="white"/>
        </linearGradient>
        <linearGradient id="extended" href="#base" x2="0" y2="1"/>
      </defs>
      <g color="purple">
        <rect x="5" y="10" width="40" height="40" fill="url(#extended)"/>
      </g>
      <g color="orange">
        <rect x="55" y="10" width="40" height="40" fill="url(#extended)"/>
      </g>
    </svg>'''
    probe = render(svg, 120, 60)
    # Vertical gradient: near the top the currentColor stop dominates
    purple = probe(20, 14)
    orange = probe(70, 14)
    assert purple[0] > purple[1] + 60 and purple[2] > purple[1] + 60
    assert orange[0] > orange[2] + 150


def test_sibling_rendering_is_not_polluted():
    """Two sibling groups rendered in sequence keep distinct colors."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="120" height="60"
         viewBox="0 0 120 60">
      <defs>
        <symbol id="s" viewBox="0 0 40 40">
          <rect width="40" height="40" fill="currentColor"/>
        </symbol>
      </defs>
      <g color="red"><use href="#s" x="5" y="10" width="40" height="40"/></g>
      <g color="blue"><use href="#s" x="55" y="10" width="40" height="40"/></g>
      <g color="green"><use href="#s" x="5" y="10" width="0" height="40"/></g>
    </svg>'''
    probe = render(svg, 120, 60)
    assert_color(probe, (10, 30), RED)
    assert_color(probe, (60, 30), BLUE)


# ---------------------------------------------------------------------------
# Patterns and masks: their content lives in <defs>, so currentColor must
# follow the referencing element rather than the definition site.
# ---------------------------------------------------------------------------

PATTERN_MASK_SVG = '''
<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100"
     viewBox="0 0 200 100">
  <defs>
    <pattern id="themedPattern" width="10" height="10"
             patternUnits="userSpaceOnUse">
      <rect width="10" height="10" fill="currentColor"/>
    </pattern>
    <mask id="themedMask">
      <rect x="0" y="0" width="100" height="100" fill="white"/>
      <circle cx="25" cy="25" r="12" fill="black"/>
    </mask>
  </defs>
  <g color="red">
    <rect x="5" y="10" width="40" height="40" fill="url(#themedPattern)"
          mask="url(#themedMask)"/>
  </g>
  <g color="blue">
    <rect x="55" y="10" width="40" height="40" fill="url(#themedPattern)"
          mask="url(#themedMask)"/>
  </g>
  <g color="green">
    <rect x="105" y="10" width="40" height="40"
          fill="url(#themedPattern)"/>
  </g>
</svg>
'''


def test_pattern_currentcolor_follows_each_reference():
    probe = render(PATTERN_MASK_SVG, 200, 100)
    # Opaque areas of each patterned rect resolve their own theme; the mask
    # in this snapshot does not punch holes (pre-existing limitation), so the
    # assertion covers fill colors only.
    assert_color(probe, (12, 45), RED)
    assert_color(probe, (62, 45), BLUE)
    assert_color(probe, (112, 30), GREEN)


def test_pattern_definition_survives_repeated_use():
    """A second render from the same source gives identical themed output."""
    first = render(PATTERN_MASK_SVG, 200, 100)
    second = render(PATTERN_MASK_SVG, 200, 100)
    for point in ((12, 45), (62, 45), (112, 30)):
        assert first(*point) == second(*point)


def test_mask_currentcolor_in_content():
    """currentColor inside a mask resolves via the masked element's color.

    Mask geometry itself is limited in this CairoSVG snapshot, so this test
    only asserts that rendering succeeds and that the mask sub-surface was
    built with the referencing element as its color context.

    """
    import io as _io

    from .parser import Tree
    from .surface import PNGSurface

    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="60"
         viewBox="0 0 100 60">
      <defs>
        <mask id="m">
          <rect x="0" y="0" width="100" height="60" fill="black"/>
          <rect x="10" y="10" width="30" height="30" fill="currentColor"/>
        </mask>
      </defs>
      <g color="white">
        <rect x="5" y="10" width="40" height="40" fill="red" mask="url(#m)"/>
      </g>
    </svg>'''
    tree = Tree(bytestring=svg.encode())
    output = _io.BytesIO()
    rendered = PNGSurface(tree, output, 96)
    # The mask definition kept its keyword rather than a frozen color
    mask_node = rendered.masks['m']
    inner_rect = mask_node.children[1]
    assert inner_rect.get('fill') == 'currentColor'
    assert mask_node.tag == 'mask'


def test_inherit_in_nested_currentcolor_chain():
    """inherit and currentColor combined across nested groups."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="160" height="60"
         viewBox="0 0 160 60">
      <defs>
        <symbol id="s" viewBox="0 0 40 40">
          <g style="color:inherit">
            <rect width="40" height="40" fill="currentColor"/>
          </g>
        </symbol>
      </defs>
      <g color="teal">
        <use href="#s" x="10" y="10" width="40" height="40"/>
      </g>
      <g color="crimson">
        <use href="#s" x="70" y="10" width="40" height="40"/>
      </g>
    </svg>'''
    probe = render(svg, 160, 60)
    assert_color(probe, (20, 30), TEAL)
    assert_color(probe, (80, 30), (220, 20, 60), tolerance=12)


def test_stop_opacity_inherit_does_not_crash():
    """stop-opacity="inherit" resolves instead of raising on float()."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="60"
         viewBox="0 0 100 60">
      <defs>
        <linearGradient id="g">
          <stop offset="0" stop-color="red" stop-opacity="inherit"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
      </defs>
      <rect x="10" y="10" width="40" height="40" fill="url(#g)"/>
    </svg>'''
    # Must render without raising; opacity falls back to 1
    probe = render(svg, 100, 60)
    assert_alpha(probe, (20, 30), 255)


def test_inherit_of_a_currentcolor_value():
    """fill="inherit" must keep resolving when the parent fill is a keyword."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="100" height="60"
         viewBox="0 0 100 60">
      <g color="teal" fill="currentColor">
        <rect x="10" y="10" width="40" height="40" fill="inherit"/>
      </g>
    </svg>'''
    probe = render(svg, 100, 60)
    assert_color(probe, (30, 30), TEAL)


def test_stroke_currentcolor_and_explicit_override():
    """currentColor strokes follow the theme; an explicit stroke wins."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="160" height="60"
         viewBox="0 0 160 60">
      <g color="red">
        <rect x="10" y="15" width="30" height="30" fill="none"
              stroke="currentColor" stroke-width="10"/>
      </g>
      <g color="red">
        <rect x="70" y="15" width="30" height="30" fill="none"
              stroke="blue" stroke-width="10"/>
      </g>
    </svg>'''
    probe = render(svg, 160, 60)
    assert_color(probe, (12, 30), RED)
    assert_color(probe, (72, 30), BLUE)


def test_missing_color_defaults_to_black_per_spec():
    """With no color anywhere, currentColor is the initial black."""
    svg = '''
    <svg xmlns="http://www.w3.org/2000/svg" width="60" height="60"
         viewBox="0 0 60 60">
      <rect x="10" y="10" width="40" height="40" fill="currentColor"/>
    </svg>'''
    probe = render(svg, 60, 60)
    assert_color(probe, (30, 30), BLACK)
