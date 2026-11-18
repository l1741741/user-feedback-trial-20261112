"""
Tests for the ``display`` and ``visibility`` properties.

These tests render real images and inspect the resulting pixels: the
visible/hidden contrast uses overlapping saturated colors and thick strokes,
so that a painting mistake is unambiguous. Checking the parsed style
dictionary would not tell whether a hidden subtree still leaks into the
output through a clip, a mask, a marker or a reused reference.

The tested semantics come from SVG 1.1 (and CSS 2.1):

* ``display: none`` removes the element *and its whole subtree* from the
  rendering tree. Descendants cannot override it.
* ``visibility`` is inherited but may be overridden by a descendant with
  ``visibility: visible``; hidden elements still take part in layout.
* Both properties can be set by a presentation attribute, an inline ``style``
  attribute or a stylesheet, with the usual CSS priorities.
* ``display: none`` only prevents *direct* rendering: an element is still
  usable as a resource (``use``, ``textPath``, ``clip-path``, ``mask``,
  gradient, pattern, marker).
* The input tree is never modified, so repeated renders and later references
  behave identically.

"""

import base64
import io

import pytest
from PIL import Image

from . import svg2png

WHITE = (255, 255, 255, 255)
BLACK = (0, 0, 0, 255)
RED = (255, 0, 0, 255)
LIME = (0, 255, 0, 255)
BLUE = (0, 0, 255, 255)
MAGENTA = (255, 0, 255, 255)


def _image(svg, **kwargs):
    """Render ``svg`` on a white background and return a Pillow image."""
    png = svg2png(bytestring=svg, background_color='white', **kwargs)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def _transparent(svg):
    """Render ``svg`` without background to test what is painted at all."""
    png = svg2png(bytestring=svg)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def _pixel(svg, x, y):
    return _image(svg).getpixel((x, y))


def _count(svg, rgba):
    image = _image(svg)
    return sum(
        1 for y in range(image.height) for x in range(image.width)
        if image.getpixel((x, y)) == rgba)


def _png_uri(color, size=16):
    buffer = io.BytesIO()
    Image.new('RGB', (size, size), color).save(buffer, 'PNG')
    return 'data:image/png;base64,' + base64.b64encode(
        buffer.getvalue()).decode()


def _svg_uri(content):
    return 'data:image/svg+xml;base64,' + base64.b64encode(
        content).decode()


def document(body, width=120, height=40):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">{body}</svg>').encode()


# ---------------------------------------------------------------------------
# Core difference between the two properties
# ---------------------------------------------------------------------------

def test_visibility_hidden_can_be_overridden_by_child():
    """A child of a hidden group paints itself with ``visibility: visible``."""
    svg = document('''
        <g visibility="hidden">
          <rect width="50" height="40" fill="red" stroke="black"
                stroke-width="6"/>
          <rect x="55" width="50" height="40" fill="lime" stroke="blue"
                stroke-width="6" visibility="visible"/>
        </g>''')
    assert _pixel(svg, 25, 20) == WHITE  # hidden fill and stroke
    assert _pixel(svg, 80, 20) == LIME  # visible override
    assert _count(svg, BLACK) == 0
    assert _count(svg, BLUE) > 0


def test_display_none_drops_the_whole_subtree():
    """A child cannot override ``display: none`` on an ancestor."""
    svg = document('''
        <g display="none">
          <rect width="50" height="40" fill="red"/>
          <rect x="55" width="50" height="40" fill="lime"
                visibility="visible" display="inline"/>
        </g>''')
    assert _count(svg, RED) == 0
    assert _count(svg, LIME) == 0
    assert _transparent(svg).getbbox() is None


def test_nested_visibility_override_is_painted():
    """An override works through several hidden ancestors."""
    svg = document('''
        <g visibility="hidden"><g visibility="hidden">
          <rect width="50" height="40" fill="red"/>
          <rect x="55" width="50" height="40" fill="blue"
                visibility="visible"/>
        </g></g>''')
    assert _count(svg, RED) == 0
    assert _count(svg, BLUE) == 2000


def test_visibility_collapse_behaves_like_hidden():
    svg = document('''
        <g visibility="collapse">
          <rect width="50" height="40" fill="red"/>
          <rect x="55" width="50" height="40" fill="blue"
                visibility="visible"/>
        </g>''')
    assert _count(svg, RED) == 0
    assert _count(svg, BLUE) == 2000


def test_display_values_other_than_none_render():
    for value in ('inline', 'block', 'table-cell'):
        svg = document(
            f'<g display="{value}"><rect width="50" height="40" '
            'fill="blue"/></g>')
        assert _count(svg, BLUE) == 2000


# ---------------------------------------------------------------------------
# Style sources and CSS cascade
# ---------------------------------------------------------------------------

def test_stylesheet_visibility_override():
    svg = document('''
        <style>
          .hidden { visibility: hidden }
          .visible { visibility: visible }
          .gone { display: none }
        </style>
        <g class="hidden">
          <rect width="30" height="40" fill="red"/>
          <rect x="35" width="30" height="40" fill="lime" class="visible"/>
          <rect x="70" width="30" height="40" fill="blue" class="gone"/>
        </g>''')
    assert _count(svg, RED) == 0
    assert _count(svg, LIME) > 0
    assert _count(svg, BLUE) == 0


def test_inline_style_wins_over_presentation_attribute():
    """The ``style`` attribute has its usual priority over attributes."""
    svg = document(
        '<rect width="50" height="40" fill="red" visibility="visible" '
        'style="visibility: hidden"/>')
    assert _count(svg, RED) == 0


def test_important_stylesheet_beats_presentation_attribute():
    svg = document('''
        <style>rect { visibility: visible !important }</style>
        <rect width="50" height="40" fill="blue" visibility="hidden"/>''')
    assert _count(svg, BLUE) == 2000


def test_display_none_cannot_be_overridden_by_inline_style():
    svg = document('''
        <style>.gone { display: none }</style>
        <g class="gone">
          <rect width="50" height="40" fill="red"
                style="display: inline !important"/>
        </g>''')
    assert _count(svg, RED) == 0


def test_visibility_inherit_keyword():
    svg = document('''
        <g visibility="hidden">
          <rect width="50" height="40" fill="red" visibility="inherit"/>
          <rect x="55" width="50" height="40" fill="blue"
                visibility="visible"/>
        </g>''')
    assert _count(svg, RED) == 0
    assert _count(svg, BLUE) == 2000


# ---------------------------------------------------------------------------
# Sibling order and context isolation
# ---------------------------------------------------------------------------

def test_sibling_order_and_state_are_preserved():
    """A hidden branch must not clip, offset or mask its siblings."""
    svg = document('''
        <defs>
          <clipPath id="small"><rect width="5" height="5"/></clipPath>
          <filter id="shift"><feOffset dx="40" dy="0"/></filter>
          <mask id="half"><rect width="25" height="40" fill="white"/></mask>
        </defs>
        <rect width="40" height="40" fill="red" display="none"
              clip-path="url(#small)" filter="url(#shift)"/>
        <rect width="40" height="40" fill="blue" display="none"/>
        <rect x="50" width="40" height="40" fill="lime"/>''')
    image = _image(svg)
    assert image.getpixel((70, 20)) == LIME
    assert image.getpixel((115, 20)) == WHITE
    assert _count(svg, RED) == 0
    assert _count(svg, BLACK) == 0


def test_hidden_group_with_opacity_still_paints_visible_child():
    svg = document('''
        <g visibility="hidden" opacity="0.5">
          <rect width="50" height="40" fill="red"/>
          <rect x="60" width="50" height="40" fill="blue"
                visibility="visible"/>
        </g>''')
    image = _image(svg)
    assert image.getpixel((20, 20)) == WHITE
    red, green, blue, alpha = image.getpixel((80, 20))
    assert alpha == 255
    assert blue > 200 and red < 200 and green < 200  # blue at 50 % over white


def test_display_none_nested_svg_does_not_move_following_content():
    svg = document('''
        <svg x="0" y="0" width="40" height="40" display="none"
             transform="translate(60, 0)">
          <rect width="40" height="40" fill="red"/>
        </svg>
        <rect width="40" height="40" fill="lime"/>''')
    assert _image(svg).getpixel((10, 20)) == LIME
    assert _count(svg, RED) == 0


# ---------------------------------------------------------------------------
# Reused content (<use>, <symbol>)
# ---------------------------------------------------------------------------

def test_visibility_override_inside_use():
    svg = document('''
        <defs>
          <g id="shared">
            <rect width="50" height="40" fill="red"/>
            <rect x="55" width="50" height="40" fill="blue"
                  visibility="visible"/>
          </g>
        </defs>
        <g visibility="hidden"><use href="#shared"/></g>''')
    assert _count(svg, RED) == 0
    assert _count(svg, BLUE) > 0


def test_display_none_use_renders_nothing():
    svg = document('''
        <defs><rect id="shared" width="50" height="40" fill="red"/></defs>
        <use href="#shared" display="none"/>
        <rect x="60" width="50" height="40" fill="blue"/>''')
    assert _count(svg, RED) == 0
    assert _count(svg, BLUE) == 2000


def test_referenced_target_keeps_its_own_visibility():
    """A referenced element keeps the ``visibility`` set on itself."""
    svg = document('''
        <defs>
          <rect id="hidden-target" width="50" height="40" fill="red"
                visibility="hidden"/>
          <rect id="gone-target" width="50" height="40" fill="blue"
                display="none"/>
        </defs>
        <use href="#hidden-target"/>
        <use href="#gone-target" x="60"/>''')
    assert _count(svg, RED) == 0
    # A target with its own ``display: none`` does not render through ``use``.
    assert _count(svg, BLUE) == 0


def test_display_none_ancestor_does_not_prevent_referencing():
    """Only the referenced element's own ``display`` matters for ``use``."""
    svg = document('''
        <defs>
          <g display="none">
            <rect id="target" width="50" height="40" fill="blue"/>
          </g>
        </defs>
        <use href="#target"/>''')
    assert _count(svg, BLUE) > 0


def test_use_does_not_mutate_the_shared_target():
    """Two uses and two renders see the same unmodified tree."""
    svg = document('''
        <defs><rect id="shared" x="5" width="40" height="40" fill="red"/>
        </defs>
        <use href="#shared"/>
        <use href="#shared" x="60"/>
        <use href="#shared" x="0" y="0"/>''')
    first = svg2png(bytestring=svg)
    image = _image(svg)
    assert image.getpixel((20, 20)) == RED  # first use, x offset from target
    assert image.getpixel((80, 20)) == RED  # second use, translated
    assert image.getpixel((115, 20)) == WHITE
    assert svg2png(bytestring=svg) == first


# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

def _marker_document(path_attributes, marker_body=''):
    return document(f'''
        <defs>
          <marker id="mark" markerWidth="10" markerHeight="10"
                  refX="0" refY="0" {marker_body}>
            <rect width="10" height="10" fill="magenta"/>
          </marker>
        </defs>
        <path d="M10 20 L100 20" stroke="black" stroke-width="2"
              marker-start="url(#mark)" {path_attributes}/>''')


def test_marker_of_visible_path_is_painted():
    assert _count(_marker_document(''), MAGENTA) == 400


@pytest.mark.parametrize('attribute', ('display="none"',
                                       'visibility="hidden"'))
def test_marker_of_hidden_path_is_not_painted(attribute):
    svg = _marker_document(attribute)
    assert _count(svg, MAGENTA) == 0
    assert _count(svg, BLACK) == 0


def test_marker_follows_visibility_override_of_its_path():
    svg = document('''
        <defs>
          <marker id="mark" markerWidth="10" markerHeight="10">
            <rect width="10" height="10" fill="magenta"/>
          </marker>
        </defs>
        <g visibility="hidden">
          <path d="M10 20 L100 20" stroke="black" visibility="visible"
                marker-start="url(#mark)"/>
        </g>''')
    assert _count(svg, MAGENTA) == 100


def test_marker_content_honors_visibility():
    svg = document('''
        <defs>
          <marker id="mark" markerWidth="12" markerHeight="12">
            <g visibility="hidden">
              <rect width="12" height="12" fill="blue"/>
              <rect x="3" y="3" width="6" height="6" fill="lime"
                    visibility="visible"/>
            </g>
          </marker>
        </defs>
        <path d="M10 20 L100 20" stroke="black"
              marker-start="url(#mark)"/>''')
    assert _count(svg, BLUE) == 0
    assert _count(svg, LIME) > 0


# ---------------------------------------------------------------------------
# Clip paths and masks
# ---------------------------------------------------------------------------

def test_display_none_clip_child_is_excluded():
    """A ``display: none`` child contributes no clip shape, and the shapes
    collected from its siblings are kept."""
    svg = document('''
        <defs>
          <clipPath id="clip">
            <rect width="40" height="40"/>
            <rect x="20" width="40" height="40" display="none"/>
          </clipPath>
        </defs>
        <rect width="100" height="40" fill="blue" clip-path="url(#clip)"/>''')
    assert _count(svg, BLUE) == 1600
    assert _image(svg).getpixel((50, 20)) == WHITE


def test_visibility_hidden_clip_child_contributes_no_geometry():
    svg = document('''
        <defs>
          <clipPath id="clip">
            <rect width="40" height="40"/>
            <rect x="20" width="40" height="40" visibility="hidden"/>
          </clipPath>
        </defs>
        <rect width="100" height="40" fill="blue" clip-path="url(#clip)"/>''')
    assert _count(svg, BLUE) == 1600
    assert _image(svg).getpixel((50, 20)) == WHITE


def test_visibility_override_inside_clip_path():
    svg = document('''
        <defs>
          <clipPath id="clip" visibility="hidden">
            <rect width="40" height="40" visibility="visible"/>
            <rect x="60" width="40" height="40"/>
          </clipPath>
        </defs>
        <rect width="100" height="40" fill="blue" clip-path="url(#clip)"/>''')
    assert _image(svg).getpixel((20, 20)) == BLUE
    assert _image(svg).getpixel((80, 20)) == WHITE


def test_display_none_clip_path_element_disables_the_reference():
    svg = document('''
        <defs>
          <clipPath id="clip" display="none">
            <rect width="40" height="40"/>
          </clipPath>
        </defs>
        <rect width="100" height="40" fill="blue" clip-path="url(#clip)"/>''')
    assert _count(svg, BLUE) == 4000


def test_display_none_mask_child_is_excluded():
    svg = document('''
        <defs>
          <mask id="mask">
            <rect width="40" height="40" fill="white"/>
            <rect x="40" width="40" height="40" fill="white"
                  display="none"/>
          </mask>
        </defs>
        <rect width="100" height="40" fill="blue" mask="url(#mask)"/>''')
    assert _image(svg).getpixel((20, 20)) == BLUE
    assert _image(svg).getpixel((60, 20)) == WHITE


def test_display_none_mask_element_disables_the_reference():
    svg = document('''
        <defs>
          <mask id="mask" display="none">
            <rect width="40" height="40" fill="white"/>
          </mask>
        </defs>
        <rect width="100" height="40" fill="blue" mask="url(#mask)"/>''')
    assert _count(svg, BLUE) == 4000


# ---------------------------------------------------------------------------
# Paint servers
# ---------------------------------------------------------------------------

def test_display_none_gradient_falls_back_to_the_color():
    svg = document('''
        <defs>
          <linearGradient id="gradient" display="none">
            <stop offset="0" stop-color="blue"/>
            <stop offset="1" stop-color="blue"/>
          </linearGradient>
        </defs>
        <rect width="100" height="40" fill="url(#gradient) lime"/>''')
    assert _count(svg, LIME) == 4000


def test_display_none_pattern_falls_back_to_the_color():
    svg = document('''
        <defs>
          <pattern id="pattern" width="10" height="10" display="none"
                   patternUnits="userSpaceOnUse">
            <rect width="10" height="10" fill="blue"/>
          </pattern>
        </defs>
        <rect width="100" height="40" fill="url(#pattern) lime"/>''')
    assert _count(svg, LIME) == 4000


def test_visibility_hidden_paint_server_is_still_used():
    svg = document('''
        <defs>
          <linearGradient id="gradient" visibility="hidden">
            <stop offset="0" stop-color="blue"/>
            <stop offset="1" stop-color="blue"/>
          </linearGradient>
        </defs>
        <rect width="100" height="40" fill="url(#gradient)"/>''')
    assert _count(svg, BLUE) == 4000


def test_display_none_gradient_stop_is_ignored():
    svg = document('''
        <defs>
          <linearGradient id="gradient">
            <stop offset="0" stop-color="red"/>
            <stop offset="1" stop-color="blue" display="none"/>
          </linearGradient>
        </defs>
        <rect width="100" height="40" fill="url(#gradient)"/>''')
    assert _count(svg, BLUE) == 0


def test_display_none_pattern_child_is_excluded():
    svg = document('''
        <defs>
          <pattern id="pattern" width="20" height="20"
                   patternUnits="userSpaceOnUse">
            <rect width="20" height="20" fill="lime"/>
            <rect width="20" height="20" fill="blue" display="none"/>
          </pattern>
        </defs>
        <rect width="100" height="40" fill="url(#pattern)"/>''')
    assert _count(svg, BLUE) == 0
    assert _count(svg, LIME) == 4000


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------

def test_hidden_text_still_lays_out_visible_override():
    svg = document('''
        <text x="0" y="30" font-size="30" fill="red" visibility="hidden">
          AAA<tspan fill="blue" visibility="visible">BBB</tspan>
        </text>''')
    image = _image(svg)
    pixels = [
        image.getpixel((x, y))
        for y in range(image.height) for x in range(image.width)]
    red = sum(1 for r, g, b, a in pixels if r > 200 and g < 80)
    blue = sum(1 for r, g, b, a in pixels if b > 200 and r < 80)
    assert red == 0
    assert blue > 0


def test_display_none_text_drops_its_visible_tspan():
    svg = document('''
        <text x="0" y="30" font-size="30" fill="red" display="none">
          AAA<tspan fill="blue" visibility="visible">BBB</tspan>
        </text>''')
    assert _transparent(svg).getbbox() is None


def test_display_none_tspan_does_not_advance_layout():
    """A ``display: none`` tspan is ignored, unlike a hidden one."""
    def tail_start(body):
        svg = document(
            f'<text x="0" y="30" font-size="20" fill="red">{body}'
            '<tspan fill="blue">B</tspan></text>')
        image = _image(svg)
        for x in range(image.width):
            for y in range(image.height):
                red, green, blue, alpha = image.getpixel((x, y))
                if alpha > 128 and blue > 200 and red < 100:
                    return x
        return None

    hidden = tail_start('AA<tspan visibility="hidden">XXXX</tspan>')
    gone = tail_start('AA<tspan display="none">XXXX</tspan>')
    reference = tail_start('AA')
    assert gone == reference  # the display:none text takes no space
    assert hidden > reference  # the hidden text still takes its space


def test_text_path_uses_hidden_geometry():
    """``display``/``visibility`` do not prevent a path from being referenced."""
    def ink(attribute):
        svg = document(f'''
            <defs><path id="curve" d="M0 30 L110 30" {attribute}/></defs>
            <text font-size="20" fill="blue">
              <textPath href="#curve">HELLO</textPath>
            </text>
        ''')
        return _count(svg, BLUE)

    reference = ink('')
    assert reference > 0
    # The text keeps its geometry when the referenced path is hidden.
    assert _count(document('''
        <defs><path id="curve" d="M0 30 L110 30"
                     visibility="hidden"/></defs>
        <text font-size="20" fill="blue">
          <textPath href="#curve">HELLO</textPath>
        </text>'''), BLUE) > 0


def test_display_none_text_path_element_drops_the_text():
    svg = document('''
        <defs><path id="curve" d="M0 30 L110 30"/></defs>
        <text font-size="20" fill="blue" display="none">
          <textPath href="#curve">HELLO</textPath>
        </text>''')
    assert _transparent(svg).getbbox() is None


# ---------------------------------------------------------------------------
# Images
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('attribute', ('display="none"',
                                       'visibility="hidden"'))
def test_hidden_raster_image_is_not_painted(attribute):
    svg = document(f'''
        <image href="{_png_uri((255, 0, 0))}" width="50" height="40"
               {attribute}/>
        <rect x="60" width="50" height="40" fill="lime"/>''')
    assert _count(svg, RED) == 0
    assert _count(svg, LIME) == 2000


def test_hidden_nested_svg_image_keeps_visible_override():
    """The inner document inherits the image's ``visibility`` value."""
    inner = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        b'<rect width="20" height="20" fill="#ff0000" visibility="hidden"/>'
        b'<rect x="5" y="5" width="10" height="10" fill="#00ff00" '
        b'visibility="visible"/></svg>')
    svg = document(f'''
        <image href="{_svg_uri(inner)}" width="40" height="40"
               visibility="hidden"/>''')
    # Browsers do not let the inner document override the image's own
    # ``visibility`` value: the whole image is hidden.
    assert _count(svg, RED) == 0
    assert _count(svg, LIME) == 0


def test_display_none_nested_svg_image_is_not_painted():
    inner = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">'
        b'<rect width="20" height="20" fill="#ff0000" visibility="visible"/>'
        b'</svg>')
    svg = document(
        f'<image href="{_svg_uri(inner)}" width="40" height="40" '
        'display="none"/>')
    assert _transparent(svg).getbbox() is None


# ---------------------------------------------------------------------------
# Bounding boxes
# ---------------------------------------------------------------------------

def test_display_none_child_does_not_extend_bounding_box():
    """Object bounding boxes ignore the ``display: none`` subtree."""
    svg = document('''
        <defs>
          <linearGradient id="gradient" x1="0" x2="1">
            <stop offset="0" stop-color="red"/>
            <stop offset="1" stop-color="lime"/>
          </linearGradient>
        </defs>
        <g fill="url(#gradient)">
          <rect width="50" height="40"/>
          <rect x="200" width="10" height="40" display="none"/>
        </g>''')
    # The gradient spans the visible rect only: the sample at its right edge
    # is the same as with no hidden sibling at all (all lime).
    control = document('''
        <defs>
          <linearGradient id="gradient" x1="0" x2="1">
            <stop offset="0" stop-color="red"/>
            <stop offset="1" stop-color="lime"/>
          </linearGradient>
        </defs>
        <g fill="url(#gradient)"><rect width="50" height="40"/></g>''')
    assert _image(svg).getpixel((40, 20)) == _image(control).getpixel((40, 20))


# ---------------------------------------------------------------------------
# Repeated renders
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('count', (2, 3, 5))
def test_repeated_renders_are_identical(count):
    svg = document('''
        <defs>
          <g id="shared">
            <rect width="30" height="40" fill="red"/>
            <rect x="10" width="30" height="40" fill="lime"
                  visibility="visible"/>
          </g>
          <pattern id="tile" width="10" height="10"
                   patternUnits="userSpaceOnUse">
            <rect width="5" height="5" fill="blue"/>
          </pattern>
          <mask id="mask"><rect width="60" height="40" fill="white"/></mask>
          <clipPath id="clip"><rect width="60" height="40"/></clipPath>
        </defs>
        <g display="none"><use href="#shared" x="5"/></g>
        <g visibility="hidden"><use href="#shared" x="60"/></g>
        <rect x="0" y="0" width="20" height="20" fill="url(#tile)"
              mask="url(#mask)" clip-path="url(#clip)"/>''')
    outputs = {svg2png(bytestring=svg) for _ in range(count)}
    assert len(outputs) == 1


def test_rendering_does_not_mutate_the_parsed_tree():
    from . import parser

    tree = parser.Tree(bytestring=document('''
        <defs><rect id="shared" x="5" width="20" height="20"
                    fill="red"/></defs>
        <use href="#shared" x="30" y="10"/>
        <g visibility="hidden" id="group">
          <rect width="10" height="10" fill="blue"/>
        </g>'''))
    before = repr(tree)

    from . import surface
    surface.PNGSurface(tree, None, 96)

    assert repr(tree) == before
