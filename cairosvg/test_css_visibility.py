"""
Tests for the distinction between ``display`` and ``visibility``.

These tests render SVGs and inspect the actual pixels instead of checking the
parsed style dictionaries: the visible/hidden contrast is built with
overlapping saturated colors and thick strokes so that a painting mistake is
unambiguous.

"""

import base64
import io

import cairocffi as cairo
import pytest
from PIL import Image

from . import svg2png

WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
RED = (255, 0, 0, 255)
LIME = (0, 255, 0, 255)
BLUE = (0, 0, 255, 255)


def _pixels(svg, **kwargs):
    png = svg2png(bytestring=svg, **kwargs)
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def _pixel(surface, x, y):
    """Return the RGBA value of a pixel (cairo stores ARGB32 as BGRA)."""
    data = bytes(surface.get_data())
    offset = y * surface.get_stride() + x * 4
    return data[offset + 2], data[offset + 1], data[offset], data[offset + 3]


def _count(surface, rgba):
    """Count the pixels exactly matching ``rgba``."""
    data = bytes(surface.get_data())
    return sum(
        1 for offset in range(0, len(data), 4)
        if (data[offset + 2], data[offset + 1], data[offset],
            data[offset + 3]) == rgba)


def _assert_absent(surface, rgba):
    assert _count(surface, rgba) == 0, (
        f'{rgba} should not be painted, found {_count(surface, rgba)} pixels')


def _assert_present(surface, rgba):
    assert _count(surface, rgba) > 0, f'{rgba} should be painted'


# ---------------------------------------------------------------------------
# Core semantics
# ---------------------------------------------------------------------------

def test_visibility_hidden_inherited_but_overridable():
    """Hidden descendants can set ``visibility: visible`` to be painted."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="200" height="120">
          <rect width="200" height="120" fill="white"/>
          <g visibility="hidden">
            <rect x="10" y="10" width="80" height="50" fill="red"
                  stroke="black" stroke-width="6"/>
            <rect x="30" y="20" width="80" height="30" fill="lime"
                  stroke="blue" stroke-width="6" visibility="visible"/>
          </g>
        </svg>''')
    # The hidden rect and its thick stroke are not painted...
    _assert_absent(surface, RED)
    _assert_absent(surface, BLACK)
    # ...but the explicit visible override is painted with its stroke.
    _assert_present(surface, LIME)
    _assert_present(surface, BLUE)
    assert _pixel(surface, 50, 35) == LIME


def test_display_none_drops_whole_subtree():
    """A ``display: none`` node and every descendant disappear."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="200" height="120">
          <rect width="200" height="120" fill="white"/>
          <g display="none">
            <rect x="10" y="10" width="80" height="50" fill="red"/>
            <rect x="30" y="20" width="80" height="30" fill="lime"
                  visibility="visible"/>
          </g>
        </svg>''')
    _assert_absent(surface, RED)
    _assert_absent(surface, LIME)


def test_visibility_hidden_does_not_disable_layout():
    """Hidden nodes keep contributing to text layout (inheritance of text)."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="300" height="80">
          <rect width="300" height="80" fill="white"/>
          <text x="10" y="50" font-size="30" visibility="hidden" fill="red">
            AAA<tspan fill="lime" visibility="visible">BBB</tspan>
          </text>
        </svg>''')
    _assert_absent(surface, RED)
    _assert_present(surface, LIME)


def test_display_none_text_drops_tspan_overrides():
    """``display: none`` on text hides visible overrides in the subtree too."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="300" height="80">
          <rect width="300" height="80" fill="white"/>
          <text x="10" y="50" font-size="30" display="none" fill="red">
            AAA<tspan fill="lime" visibility="visible">BBB</tspan>
          </text>
        </svg>''')
    _assert_absent(surface, RED)
    _assert_absent(surface, LIME)


def test_sibling_order_preserved():
    """Skipping a node does not change how later siblings paint."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="140" height="70">
          <rect width="140" height="70" fill="white"/>
          <rect x="10" y="10" width="50" height="50" fill="red"
                display="none"/>
          <rect x="10" y="10" width="50" height="50" fill="lime"/>
          <rect x="80" y="10" width="50" height="50" fill="lime"
                visibility="hidden"/>
          <rect x="80" y="10" width="50" height="50" fill="blue"/>
        </svg>''')
    assert _pixel(surface, 35, 35) == LIME
    assert _pixel(surface, 105, 35) == BLUE
    _assert_absent(surface, RED)


# ---------------------------------------------------------------------------
# Style sources and cascade
# ---------------------------------------------------------------------------

def test_stylesheet_and_inline_cascade():
    """Stylesheet, inline style and attribute rules keep CSS priorities."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="240" height="90">
          <style>
            .hidden { visibility: hidden; }
            .forced { visibility: visible !important; }
            .gone { display: none; }
          </style>
          <rect width="240" height="90" fill="white"/>
          <g class="hidden">
            <rect x="5" y="10" width="50" height="50" fill="red"/>
            <rect x="65" y="10" width="50" height="50" fill="lime"
                  class="forced"/>
            <rect x="125" y="10" width="50" height="50" fill="blue"
                  style="visibility: visible"/>
          </g>
          <rect x="185" y="10" width="50" height="50" fill="red"
                class="gone"/>
        </svg>''')
    _assert_absent(surface, RED)
    _assert_present(surface, LIME)
    _assert_present(surface, BLUE)


def test_inline_style_overrides_attribute():
    """A ``style`` attribute wins over the presentation attribute."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
          <rect width="80" height="80" fill="white"/>
          <rect x="10" y="10" width="60" height="60" fill="red"
                visibility="visible" style="visibility: hidden"/>
        </svg>''')
    _assert_absent(surface, RED)


def test_visibility_collapse_behaves_like_hidden():
    """``visibility: collapse`` is equivalent to ``hidden`` in SVG."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
          <rect width="80" height="80" fill="white"/>
          <rect x="10" y="10" width="60" height="60" fill="red"
                visibility="collapse"/>
        </svg>''')
    _assert_absent(surface, RED)


def test_visibility_inherit_keyword():
    """An explicit ``inherit`` follows the parent's computed visibility."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">
          <rect width="120" height="80" fill="white"/>
          <g visibility="hidden">
            <rect x="5" y="5" width="40" height="40" fill="red"
                  visibility="inherit"/>
            <rect x="60" y="5" width="40" height="40" fill="lime"
                  visibility="visible"/>
          </g>
        </svg>''')
    _assert_absent(surface, RED)
    _assert_present(surface, LIME)


def test_display_block_and_other_values_render():
    """Only ``display: none`` disables rendering; other values are neutral."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
          <rect width="80" height="80" fill="white"/>
          <rect x="10" y="10" width="60" height="60" fill="blue"
                display="block"/>
        </svg>''')
    _assert_present(surface, BLUE)


def test_deeply_nested_visibility_override():
    """A visible node can escape several levels of hidden ancestors."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">
          <rect width="120" height="80" fill="white"/>
          <g visibility="hidden">
            <g visibility="hidden">
              <rect x="5" y="5" width="40" height="40" fill="red"/>
              <rect x="60" y="5" width="40" height="40" fill="lime"
                    visibility="visible"/>
            </g>
          </g>
        </svg>''')
    _assert_absent(surface, RED)
    _assert_present(surface, LIME)


def test_important_stylesheet_beats_hidden_attribute():
    """CSS cascade priority is preserved (important rule wins)."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
          <style>rect { visibility: visible !important; }</style>
          <rect width="80" height="80" fill="white"/>
          <rect x="10" y="10" width="60" height="60" fill="red"
                visibility="hidden"/>
        </svg>''')
    _assert_present(surface, RED)


def test_basic_shapes_display_none():
    """All geometry tags honor display:none, not just rectangles."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="140" height="70">
          <rect width="140" height="70" fill="white"/>
          <circle cx="20" cy="35" r="15" fill="red" display="none"/>
          <ellipse cx="65" cy="35" rx="15" ry="12" fill="lime"
                   display="none"/>
          <line x1="100" y1="15" x2="130" y2="55" stroke="blue"
                stroke-width="4" display="none"/>
        </svg>''')
    _assert_absent(surface, RED)
    _assert_absent(surface, LIME)
    _assert_absent(surface, BLUE)


def test_textpath_under_display_none_absent():
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
          <defs>
            <path id="tp" d="M10 50 C 60 10, 140 90, 190 50"/>
          </defs>
          <rect width="200" height="100" fill="white"/>
          <text display="none" fill="blue" font-size="20">
            <textPath href="#tp">should disappear</textPath>
          </text>
        </svg>''')
    _assert_absent(surface, BLUE)


# ---------------------------------------------------------------------------
# Reused nodes (<use>)
# ---------------------------------------------------------------------------

def test_use_inside_hidden_branch_overridable():
    """Visibility overrides inside a referenced tree still paint."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">
          <defs>
            <g id="symbol">
              <rect x="0" y="0" width="40" height="40" fill="red"/>
              <rect x="10" y="10" width="40" height="20" fill="lime"
                    visibility="visible"/>
            </g>
          </defs>
          <rect width="200" height="100" fill="white"/>
          <g visibility="hidden">
            <use href="#symbol" x="10" y="10"/>
          </g>
        </svg>''')
    _assert_absent(surface, RED)
    _assert_present(surface, LIME)


def test_use_inside_display_none_dropped_without_mutating_tree():
    """A hidden ``use`` must not alter the shared target for later uses."""
    svg = b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="220" height="120">
          <defs>
            <g id="shared">
              <rect x="0" y="0" width="40" height="40" fill="red"/>
              <rect x="5" y="5" width="20" height="20" fill="lime"/>
            </g>
          </defs>
          <rect width="220" height="120" fill="white"/>
          <g display="none">
            <use href="#shared" x="10" y="10"/>
          </g>
          <use href="#shared" x="100" y="40"/>
        </svg>'''
    first = svg2png(bytestring=svg)
    surface = _pixels(svg)
    # The second use paints the shared content untouched: red where only the
    # red rect lies, lime where the inner rect overlaps it.
    assert _pixel(surface, 130, 50) == RED
    assert _pixel(surface, 115, 50) == LIME
    # The display:none branch painted nothing at its own location.
    assert _pixel(surface, 30, 30) == WHITE
    # ...and rendering again gives a byte-identical result (no state leak).
    assert svg2png(bytestring=svg) == first


def test_use_target_display_none_is_skipped():
    """A referenced root with ``display: none`` renders nothing."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="180" height="80">
          <defs>
            <g id="hidden-symbol" display="none">
              <rect width="40" height="40" fill="red"/>
            </g>
          </defs>
          <rect width="180" height="80" fill="white"/>
          <use href="#hidden-symbol" x="10" y="10"/>
          <rect x="100" y="10" width="40" height="40" fill="blue"/>
        </svg>''')
    _assert_absent(surface, RED)
    _assert_present(surface, BLUE)


# ---------------------------------------------------------------------------
# References: markers, clips, masks, patterns, gradients
# ---------------------------------------------------------------------------

def test_marker_of_hidden_path_not_painted():
    """Markers are part of the path's own painting and must be hidden too."""
    def render(hidden):
        extra = ' visibility="hidden"' if hidden else ''
        return _pixels(f'''
            <svg xmlns="http://www.w3.org/2000/svg" width="220" height="110">
              <defs>
                <marker id="marker" markerWidth="10" markerHeight="10"
                        refX="5" refY="5">
                  <rect width="10" height="10" fill="blue"/>
                </marker>
              </defs>
              <rect width="220" height="110" fill="white"/>
              <line x1="20" y1="25" x2="80" y2="25" stroke="black"
                    stroke-width="2" marker-end="url(#marker)"{extra}/>
              <line x1="20" y1="80" x2="80" y2="80" stroke="black"
                    stroke-width="2" marker-end="url(#marker)"/>
            </svg>'''.encode())
    # With the first line visible, two identical markers are painted; hiding
    # it must halve the painted blue area (no half-transparent leftovers).
    visible = _count(render(hidden=False), BLUE)
    hidden = _count(render(hidden=True), BLUE)
    assert hidden * 2 == visible
    # The hidden line's own stroke disappears as well (only the visible
    # reference line remains), again halving the painted black area.
    assert _count(render(hidden=True), BLACK) * 2 == \
        _count(render(hidden=False), BLACK)


def test_marker_of_display_none_path_not_painted():
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">
          <defs>
            <marker id="marker2" markerWidth="10" markerHeight="10"
                    refX="5" refY="5">
              <rect width="10" height="10" fill="blue"/>
            </marker>
          </defs>
          <rect width="120" height="80" fill="white"/>
          <line x1="20" y1="30" x2="80" y2="30" stroke="black"
                stroke-width="2" marker-end="url(#marker2)"
                display="none"/>
        </svg>''')
    _assert_absent(surface, BLUE)
    _assert_absent(surface, BLACK)


def test_visibility_override_inside_marker_content():
    """The visibility distinction also applies to marker content itself."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="120" height="80">
          <defs>
            <marker id="marker3" markerWidth="12" markerHeight="12"
                    refX="6" refY="6">
              <g visibility="hidden">
                <rect width="12" height="12" fill="blue"/>
                <rect x="3" y="3" width="6" height="6" fill="lime"
                      visibility="visible"/>
              </g>
            </marker>
          </defs>
          <rect width="120" height="80" fill="white"/>
          <line x1="20" y1="40" x2="60" y2="40" stroke="black"
                stroke-width="2" marker-end="url(#marker3)"/>
        </svg>''')
    _assert_absent(surface, BLUE)
    _assert_present(surface, LIME)


def test_display_none_child_excluded_from_clip_path():
    """Only displayed children of a clipPath define the clipping region."""
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <defs>
            <clipPath id="clip">
              <g display="none">
                <rect x="0" y="0" width="20" height="20"/>
              </g>
              <rect x="10" y="10" width="60" height="60"/>
            </clipPath>
          </defs>
          <rect width="100" height="100" fill="white"/>
          <rect width="100" height="100" fill="red"
                clip-path="url(#clip)"/>
        </svg>''')
    assert _pixel(surface, 5, 5) == WHITE
    assert _pixel(surface, 40, 40) == RED


def test_display_none_child_excluded_from_mask():
    # A display:none white mask rect must not let the masked red through:
    # the same rect without display:none exposes a 30x30 red square.
    def red_pixels(display_none):
        extra = ' display="none"' if display_none else ''
        surface = _pixels(f'''
            <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
              <defs>
                <mask id="mask">
                  <rect x="10" y="10" width="30" height="30"
                        fill="white"{extra}/>
                </mask>
              </defs>
              <rect width="100" height="100" fill="red" mask="url(#mask)"/>
            </svg>'''.encode())
        return _count(surface, RED)

    assert red_pixels(display_none=False) > 0
    assert red_pixels(display_none=True) == 0


def test_display_none_child_excluded_from_pattern():
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <defs>
            <pattern id="pattern" width="20" height="20"
                     patternUnits="userSpaceOnUse">
              <rect width="20" height="20" fill="white"/>
              <rect width="20" height="20" fill="blue" display="none"/>
            </pattern>
          </defs>
          <rect width="100" height="100" fill="url(#pattern)"/>
        </svg>''')
    _assert_absent(surface, BLUE)


def test_display_none_gradient_stop_ignored():
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <defs>
            <linearGradient id="gradient" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stop-color="red"/>
              <stop offset="0.5" stop-color="blue" display="none"/>
              <stop offset="1" stop-color="lime"/>
            </linearGradient>
          </defs>
          <rect width="100" height="100" fill="url(#gradient)"/>
        </svg>''')
    _assert_absent(surface, BLUE)


# ---------------------------------------------------------------------------
# Context isolation (no layout / state leaks)
# ---------------------------------------------------------------------------

def test_display_none_group_with_opacity_does_not_swallow_siblings():
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <rect width="100" height="100" fill="white"/>
          <g display="none" opacity="0.5">
            <rect width="100" height="100" fill="black"/>
          </g>
          <rect x="20" y="20" width="60" height="60" fill="red"/>
        </svg>''')
    assert _pixel(surface, 50, 50) == RED


def test_hidden_group_with_opacity_and_visible_child():
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <rect width="100" height="100" fill="white"/>
          <g visibility="hidden" opacity="0.5">
            <rect width="100" height="100" fill="black"/>
            <rect x="20" y="20" width="60" height="60" fill="lime"
                  visibility="visible"/>
          </g>
        </svg>''')
    # The black rect is hidden; the visible lime child is blended at 50%.
    _assert_absent(surface, BLACK)
    data = bytes(surface.get_data())
    lime_pixels = [
        (data[i + 2], data[i + 1], data[i])
        for i in range(0, len(data), 4)
        if data[i + 1] > data[i + 2]]  # green channel dominates
    assert lime_pixels  # visible override did paint greenish pixels
    assert _pixel(surface, 5, 5) == WHITE


def test_display_none_clip_does_not_leak_to_siblings():
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <defs>
            <clipPath id="tiny"><rect x="0" y="0" width="5" height="5"/>
            </clipPath>
          </defs>
          <rect width="100" height="100" fill="white"/>
          <rect width="50" height="50" fill="black" display="none"
                clip-path="url(#tiny)"/>
          <rect x="10" y="10" width="80" height="80" fill="red"/>
        </svg>''')
    assert _pixel(surface, 60, 60) == RED
    _assert_absent(surface, BLACK)


def test_display_none_filter_does_not_leak_to_siblings():
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <defs>
            <filter id="offset" x="-50%" y="-50%" width="200%"
                    height="200%">
              <feOffset dx="30" dy="30"/>
            </filter>
          </defs>
          <rect width="100" height="100" fill="white"/>
          <rect width="20" height="20" fill="black" display="none"
                filter="url(#offset)"/>
          <rect x="10" y="10" width="20" height="20" fill="red"/>
        </svg>''')
    assert _pixel(surface, 15, 15) == RED
    assert _pixel(surface, 45, 45) == WHITE
    _assert_absent(surface, BLACK)


def test_nested_svg_display_none():
    surface = _pixels(b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">
          <rect width="100" height="100" fill="white"/>
          <svg x="0" y="0" width="50" height="50" display="none">
            <rect width="50" height="50" fill="red"/>
          </svg>
        </svg>''')
    _assert_absent(surface, RED)


@pytest.mark.parametrize('render_count', (2, 5))
def test_repeated_renders_are_stable(render_count):
    """Rendering multiple times must not depend on mutated input nodes."""
    svg = b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="160" height="80">
          <defs>
            <g id="reused">
              <rect width="30" height="30" fill="red"/>
            </g>
          </defs>
          <rect width="160" height="80" fill="white"/>
          <g display="none"><use href="#reused" x="5" y="5"/></g>
          <g visibility="hidden">
            <use href="#reused" x="60" y="25"/>
          </g>
          <use href="#reused" x="110" y="25"/>
        </svg>'''
    outputs = {svg2png(bytestring=svg) for _ in range(render_count)}
    assert len(outputs) == 1


# ---------------------------------------------------------------------------
# Raster and nested-SVG <image>
# ---------------------------------------------------------------------------

def _png_data_uri(rgb):
    buffer = io.BytesIO()
    Image.new('RGB', (16, 16), rgb).save(buffer, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(
        buffer.getvalue()).decode()


def _svg_data_uri(svg):
    return 'data:image/svg+xml;base64,' + base64.b64encode(svg).decode()


def test_raster_image_visibility_and_display():
    uri = _png_data_uri((255, 0, 0))
    for declaration in ('visibility="hidden"', 'display="none"'):
        surface = _pixels(f'''
            <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
              <rect width="80" height="80" fill="white"/>
              <image x="5" y="5" width="40" height="40" {declaration}
                     href="{uri}"/>
            </svg>'''.encode())
        _assert_absent(surface, RED)


def test_nested_svg_image_inherits_visibility():
    """A nested SVG image inherits the parent image's hidden visibility."""
    inner = (b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
             b'<rect width="40" height="40" fill="red"/></svg>')
    uri = _svg_data_uri(inner)
    surface = _pixels(f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
          <rect width="80" height="80" fill="white"/>
          <image x="5" y="5" width="40" height="40" visibility="hidden"
                 href="{uri}"/>
        </svg>'''.encode())
    _assert_absent(surface, RED)


def test_nested_svg_image_visible_override_inside_hidden_image():
    """A visible child of a hidden image still paints (visibility override)."""
    inner = b'''
        <svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">
          <rect width="40" height="40" fill="red" visibility="hidden"/>
          <rect x="10" y="10" width="20" height="20" fill="lime"
                visibility="visible"/>
        </svg>'''
    uri = _svg_data_uri(inner)
    surface = _pixels(f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
          <rect width="80" height="80" fill="white"/>
          <image x="5" y="5" width="40" height="40" visibility="hidden"
                 href="{uri}"/>
        </svg>'''.encode())
    _assert_absent(surface, RED)
    _assert_present(surface, LIME)


def test_nested_svg_image_display_none_drops_content():
    inner = (b'<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40">'
             b'<rect width="40" height="40" fill="red"/></svg>')
    uri = _svg_data_uri(inner)
    surface = _pixels(f'''
        <svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">
          <rect width="80" height="80" fill="white"/>
          <image x="5" y="5" width="40" height="40" display="none"
                 href="{uri}"/>
        </svg>'''.encode())
    _assert_absent(surface, RED)
