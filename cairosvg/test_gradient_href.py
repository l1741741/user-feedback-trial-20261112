"""
Tests for gradient (and pattern) inheritance through ``href``.

Brand-icon style documents share public gradient definitions via SVG2
``href`` (and the legacy ``xlink:href``). The shared registry must never be
mutated while resolving those references, otherwise one shape’s overrides
leak into every later shape using the same definition.

These tests render actual PNGs and sample pixels, comparing against
equivalent gradients written without ``href`` as regression baselines.

"""

import io

import pytest

from . import svg2png
from .defs import resolve_def_href
from .parser import Tree
from .surface import PNGSurface

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

pytestmark = pytest.mark.skipif(
    Image is None, reason='Pillow is required for pixel-sampling tests')


SVG_HEAD = (
    '<svg xmlns="http://www.w3.org/2000/svg" '
    'xmlns:xlink="http://www.w3.org/1999/xlink" '
    'width="{w}" height="{h}" viewBox="0 0 {w} {h}">')


def render(svg, width=None, height=None):
    """Render an SVG string and return an RGB PIL image."""
    if isinstance(svg, str):
        svg = svg.encode()
    png = svg2png(svg, output_width=width, output_height=height)
    return Image.open(io.BytesIO(png)).convert('RGB')


def pixels(img):
    """Raw RGB bytes of an image for exact, order-independent comparison."""
    return img.tobytes()


def surface_of(svg):
    return PNGSurface(
        Tree(bytestring=svg.encode()), io.BytesIO(), 96)


def stop_summary(node):
    return [
        (child.get('offset'), child.get('stop-color'))
        for child in node.children]


# --- Attribute and stop inheritance --------------------------------------

def test_unspecified_attributes_are_inherited():
    """Geometry given only on the base gradient reaches the referencing one."""
    svg = SVG_HEAD.format(w=10, h=10) + '''
      <defs>
        <linearGradient id="base" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
        <linearGradient id="ref" href="#base"/>
      </defs>
      <rect width="10" height="10" fill="url(#ref)"/>
    </svg>'''
    surface = surface_of(svg)
    resolved = resolve_def_href(surface, 'ref', surface.gradients)
    assert resolved.get('x1') == '0'
    assert resolved.get('x2') == '1'
    assert stop_summary(resolved) == [('0', 'red'), ('1', 'blue')]
    # Registry itself is untouched
    assert 'x2' not in surface.gradients['ref']


def test_own_attributes_override_inherited_ones():
    svg = SVG_HEAD.format(w=10, h=10) + '''
      <defs>
        <linearGradient id="base" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
        <linearGradient id="ref" href="#base" x2="0" y2="1"/>
      </defs>
      <rect width="10" height="10" fill="url(#ref)"/>
    </svg>'''
    surface = surface_of(svg)
    resolved = resolve_def_href(surface, 'ref', surface.gradients)
    assert resolved.get('x2') == '0'
    assert resolved.get('y2') == '1'
    # x1 is still inherited
    assert resolved.get('x1') == '0'


def test_own_stops_replace_inherited_stops():
    """A referencing gradient with children keeps its own stops (SVG2)."""
    svg = SVG_HEAD.format(w=10, h=10) + '''
      <defs>
        <linearGradient id="base">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
        <linearGradient id="ref" href="#base">
          <stop offset="0" stop-color="lime"/>
          <stop offset="1" stop-color="aqua"/>
        </linearGradient>
      </defs>
      <rect width="10" height="10" fill="url(#ref)"/>
    </svg>'''
    surface = surface_of(svg)
    resolved = resolve_def_href(surface, 'ref', surface.gradients)
    assert stop_summary(resolved) == [('0', 'lime'), ('1', 'aqua')]


def test_xlink_href_is_accepted():
    svg = SVG_HEAD.format(w=10, h=10) + '''
      <defs>
        <linearGradient id="base">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
        <linearGradient id="ref" xlink:href="#base"/>
      </defs>
      <rect width="10" height="10" fill="url(#ref)"/>
    </svg>'''
    surface = surface_of(svg)
    resolved = resolve_def_href(surface, 'ref', surface.gradients)
    assert stop_summary(resolved) == [('0', 'red'), ('1', 'blue')]


# --- Multi-level chains ---------------------------------------------------

def test_multilevel_chain_inherits_stops_and_attributes():
    svg = SVG_HEAD.format(w=10, h=10) + '''
      <defs>
        <linearGradient id="base" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
        <linearGradient id="mid" href="#base" spreadMethod="repeat"/>
        <linearGradient id="leaf" href="#mid"/>
      </defs>
      <rect width="10" height="10" fill="url(#leaf)"/>
    </svg>'''
    surface = surface_of(svg)
    leaf = resolve_def_href(surface, 'leaf', surface.gradients)
    assert stop_summary(leaf) == [('0', 'red'), ('1', 'blue')]
    assert leaf.get('spreadMethod') == 'repeat'
    assert leaf.get('x2') == '1'
    # Intermediate raw node stays childless in the registry
    assert surface.gradients['mid'].children == []


def test_multilevel_chain_pixels_match_direct_gradient():
    """The chain renders exactly like the base gradient written inline."""
    chain = SVG_HEAD.format(w=40, h=40) + '''
      <defs>
        <linearGradient id="base" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
        <linearGradient id="mid" href="#base"/>
        <linearGradient id="leaf" href="#mid"/>
      </defs>
      <rect width="40" height="40" fill="url(#leaf)"/>
    </svg>'''
    direct = SVG_HEAD.format(w=40, h=40) + '''
      <defs>
        <linearGradient id="base" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
      </defs>
      <rect width="40" height="40" fill="url(#base)"/>
    </svg>'''
    assert pixels(render(chain)) == pixels(render(direct))


# --- Draw order independence ---------------------------------------------

ICONS_SVG = '''
  <defs>
    <linearGradient id="brandLinear" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="#d00000"/>
      <stop offset="1" stop-color="#0033d0"/>
    </linearGradient>
    <linearGradient id="iconLinear" href="#brandLinear" x2="0" y2="1"/>
    <linearGradient id="iconChain" href="#iconLinear" spreadMethod="repeat"/>
    <radialGradient id="brandRadial" cx="50%" cy="50%" r="50%">
      <stop offset="0" stop-color="#fff2a8"/>
      <stop offset="1" stop-color="#b3541e"/>
    </radialGradient>
    <radialGradient id="iconRadial" href="#brandRadial" r="75%"/>
  </defs>
  {groups}
'''

GROUP_A = ('<g transform="translate(8,12)"><rect x="0" y="0" width="56" '
           'height="56" rx="10" fill="url(#iconLinear)"/></g>')
GROUP_B = ('<g transform="translate(92,12)" opacity="0.85"><circle cx="28" '
           'cy="28" r="28" fill="url(#iconChain)"/></g>')
GROUP_C = ('<g transform="translate(196,40) rotate(45)"><path '
           'd="M-26,-26 H26 V26 H-26 Z" fill="url(#iconRadial)"/></g>')


def icons_document(order):
    groups = ''.join({'A': GROUP_A, 'B': GROUP_B, 'C': GROUP_C}[i]
                     for i in order)
    return SVG_HEAD.format(w=240, h=80) + ICONS_SVG.format(groups=groups) + \
        '</svg>'


@pytest.mark.parametrize('order', ['CBA', 'BAC', 'ACB'])
def test_swapping_shape_order_keeps_each_icon_identical(order):
    reference = render(icons_document('ABC'))
    shuffled = render(icons_document(order))
    assert pixels(reference) == pixels(shuffled)


def test_shared_gradient_used_by_many_shapes_is_stable():
    """One definition, several shapes/transforms/opacities in one document."""
    def document(reversed_order):
        shapes = [
            '<rect x="0" y="0" width="40" height="40" fill="url(#shared)"/>',
            '<circle cx="60" cy="20" r="20" fill="url(#shared)" '
            'opacity="0.5"/>',
            '<g transform="translate(80,0) rotate(90 20 20)">'
            '<rect width="40" height="40" fill="url(#shared)"/></g>']
        if reversed_order:
            shapes = list(reversed(shapes))
        return SVG_HEAD.format(w=120, h=40) + '''
          <defs>
            <linearGradient id="shared" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stop-color="red"/>
              <stop offset="1" stop-color="blue"/>
            </linearGradient>
          </defs>
        ''' + ''.join(shapes) + '</svg>'

    img = render(document(False))
    rgba = Image.open(io.BytesIO(svg2png(
        document(False).encode(), output_width=120, output_height=40)))
    # Opaque rect: red left edge, blue right edge in bbox space.
    assert img.getpixel((2, 20))[0] > 200
    assert img.getpixel((38, 20))[2] > 200
    # The circle keeps the same gradient colors but only 50% opacity:
    # premultiplied RGB matches the opaque rect while its alpha is halved.
    circle_edge = rgba.getpixel((78, 20))
    rect_edge = rgba.getpixel((38, 20))
    assert circle_edge[:3] == rect_edge[:3]
    assert circle_edge[3] == 128 and rect_edge[3] == 255
    # The rotated rect keeps the same gradient in its own bbox: its top edge
    # (which was the left, red edge before rotation) is red.
    assert img.getpixel((100, 2))[0] > 200
    # Swapping draw order changes nothing: every shape resolves the shared
    # definition independently without mutating it.
    assert pixels(img) == pixels(render(document(True)))


# --- Coordinate systems and spread behavior ------------------------------

def test_userspace_on_use_coordinates_are_absolute():
    svg = SVG_HEAD.format(w=80, h=40) + '''
      <defs>
        <linearGradient id="u" gradientUnits="userSpaceOnUse"
                        x1="0" y1="0" x2="80" y2="0">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
        <linearGradient id="u2" href="#u"/>
      </defs>
      <rect x="0" y="0" width="40" height="40" fill="url(#u2)"/>
      <rect x="40" y="0" width="40" height="40" fill="url(#u2)"/>
    </svg>'''
    img = render(svg)
    left = img.getpixel((2, 20))
    right = img.getpixel((78, 20))
    assert left[0] > 200 and left[2] < 80      # red at document left
    assert right[2] > 200 and right[0] < 80    # blue at document right


def test_spread_method_override_repeats_inherited_stops():
    svg = SVG_HEAD.format(w=60, h=20) + '''
      <defs>
        <linearGradient id="base" x1="0" y1="0" x2="0.25" y2="0">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="yellow"/>
        </linearGradient>
        <linearGradient id="mid" href="#base"/>
        <linearGradient id="rep" href="#mid" spreadMethod="repeat"/>
      </defs>
      <rect width="60" height="20" fill="url(#rep)"/>
    </svg>'''
    img = render(svg, 60, 20)
    # Red bands restart every quarter of the width thanks to repeat.
    red_bands = sum(
        1 for x in (2, 17, 32, 47)
        if img.getpixel((x, 10))[0] > 200 and img.getpixel((x, 10))[1] < 120)
    assert red_bands >= 3


def test_radial_override_pixels_match_equivalent_inline_radial():
    href_svg = SVG_HEAD.format(w=40, h=40) + '''
      <defs>
        <radialGradient id="base" cx="50%" cy="50%" r="50%">
          <stop offset="0" stop-color="white"/>
          <stop offset="1" stop-color="black"/>
        </radialGradient>
        <radialGradient id="big" href="#base" r="75%"/>
      </defs>
      <rect width="40" height="40" fill="url(#big)"/>
    </svg>'''
    inline_svg = SVG_HEAD.format(w=40, h=40) + '''
      <defs>
        <radialGradient id="big" cx="50%" cy="50%" r="75%">
          <stop offset="0" stop-color="white"/>
          <stop offset="1" stop-color="black"/>
        </radialGradient>
      </defs>
      <rect width="40" height="40" fill="url(#big)"/>
    </svg>'''
    assert pixels(render(href_svg)) == pixels(render(inline_svg))


# --- Cycles and missing references ---------------------------------------

def test_cyclic_reference_terminates_safely():
    svg = SVG_HEAD.format(w=10, h=10) + '''
      <defs>
        <linearGradient id="a" href="#b">
          <stop offset="0" stop-color="#7b2cbf"/>
        </linearGradient>
        <linearGradient id="b" href="#a">
          <stop offset="1" stop-color="#ffd60a"/>
        </linearGradient>
      </defs>
      <rect width="10" height="10" fill="url(#a)"/>
    </svg>'''
    surface = surface_of(svg)
    resolved = resolve_def_href(surface, 'a', surface.gradients)
    assert stop_summary(resolved) == [('0', '#7b2cbf')]
    # Full render does not raise and uses the usable stop.
    img = render(svg)
    assert img.getpixel((5, 5)) == (123, 44, 191)


def test_self_reference_terminates_safely():
    svg = SVG_HEAD.format(w=10, h=10) + '''
      <defs>
        <linearGradient id="g" href="#g">
          <stop offset="0" stop-color="red"/>
        </linearGradient>
      </defs>
      <rect width="10" height="10" fill="url(#g)"/>
    </svg>'''
    surface = surface_of(svg)
    resolved = resolve_def_href(surface, 'g', surface.gradients)
    assert stop_summary(resolved) == [('0', 'red')]
    assert render(svg).getpixel((5, 5)) == (255, 0, 0)


def test_deep_chain_finds_terminal_definition():
    """Five levels of childless hrefs still resolve stops and attributes."""
    svg = SVG_HEAD.format(w=10, h=10) + '''
      <defs>
        <linearGradient id="g0" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
        <linearGradient id="g1" href="#g0"/>
        <linearGradient id="g2" href="#g1"/>
        <linearGradient id="g3" href="#g2"/>
        <linearGradient id="g4" href="#g3" spreadMethod="reflect"/>
      </defs>
      <rect width="10" height="10" fill="url(#g4)"/>
    </svg>'''
    surface = surface_of(svg)
    resolved = resolve_def_href(surface, 'g4', surface.gradients)
    assert stop_summary(resolved) == [('0', 'red'), ('1', 'blue')]
    assert resolved.get('spreadMethod') == 'reflect'
    assert resolved.get('x2') == '1'
    # The shared intermediate nodes were never given children.
    assert surface.gradients['g1'].children == []
    assert surface.gradients['g3'].children == []


def test_missing_reference_uses_node_as_is():
    svg = SVG_HEAD.format(w=10, h=10) + '''
      <defs>
        <linearGradient id="a" href="#nope">
          <stop offset="0" stop-color="#212529"/>
        </linearGradient>
      </defs>
      <rect width="10" height="10" fill="url(#a)"/>
    </svg>'''
    surface = surface_of(svg)
    resolved = resolve_def_href(surface, 'a', surface.gradients)
    assert resolved is surface.gradients['a']
    img = render(svg)
    assert img.getpixel((5, 5)) == (33, 37, 41)


def test_broken_references_do_not_pollute_healthy_gradient():
    svg = SVG_HEAD.format(w=120, h=40) + '''
      <defs>
        <linearGradient id="cyc-a" href="#cyc-b">
          <stop offset="0" stop-color="#7b2cbf"/>
        </linearGradient>
        <linearGradient id="cyc-b" href="#cyc-a">
          <stop offset="1" stop-color="#ffd60a"/>
        </linearGradient>
        <linearGradient id="missing" href="#nope">
          <stop offset="0" stop-color="#212529"/>
        </linearGradient>
        <linearGradient id="healthy" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
      </defs>
      <rect x="0" y="0" width="40" height="40" fill="url(#cyc-a)"/>
      <rect x="40" y="0" width="40" height="40" fill="url(#missing)"/>
      <rect x="80" y="0" width="40" height="40" fill="url(#healthy)"/>
    </svg>'''
    img = render(svg)
    assert img.getpixel((82, 20))[0] > 200       # healthy red edge
    assert img.getpixel((118, 20))[2] > 200      # healthy blue edge


# --- Pattern href and repeated paints ------------------------------------

def test_pattern_href_inherits_content_and_overrides_size():
    svg = SVG_HEAD.format(w=80, h=40) + '''
      <defs>
        <pattern id="base" width="10" height="10"
                 patternUnits="userSpaceOnUse">
          <rect width="10" height="10" fill="white"/>
          <circle cx="5" cy="5" r="4" fill="darkgreen"/>
        </pattern>
        <pattern id="wide" href="#base" width="20" height="10"
                 patternUnits="userSpaceOnUse"/>
      </defs>
      <rect x="0" y="0" width="40" height="40" fill="url(#base)"/>
      <rect x="40" y="0" width="40" height="40" fill="url(#wide)"/>
    </svg>'''
    img = render(svg)
    base_green = sum(
        1 for x in range(0, 40, 2) for y in range(0, 40, 2)
        if img.getpixel((x, y))[1] > 100 and img.getpixel((x, y))[0] < 100)
    wide_green = sum(
        1 for x in range(40, 80, 2) for y in range(0, 40, 2)
        if img.getpixel((x, y))[1] > 100 and img.getpixel((x, y))[0] < 100)
    assert base_green == wide_green * 2


def test_repeated_paints_are_deterministic():
    """Pattern drawing pops width/height; the shared node must stay intact."""
    svg = SVG_HEAD.format(w=40, h=40) + '''
      <defs>
        <pattern id="base" width="10" height="10"
                 patternUnits="userSpaceOnUse">
          <rect width="10" height="10" fill="white"/>
          <circle cx="5" cy="5" r="4" fill="darkgreen"/>
        </pattern>
        <pattern id="wide" href="#base" width="20" height="10"
                 patternUnits="userSpaceOnUse"/>
      </defs>
      <rect width="40" height="40" fill="url(#wide)"/>
    </svg>'''
    first = svg2png(svg.encode(), output_width=40, output_height=40)
    second = svg2png(svg.encode(), output_width=40, output_height=40)
    assert first == second


def test_registry_is_never_mutated_by_resolution():
    """Resolving any number of references leaves original nodes in place."""
    svg = SVG_HEAD.format(w=10, h=10) + '''
      <defs>
        <linearGradient id="base" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="red"/>
          <stop offset="1" stop-color="blue"/>
        </linearGradient>
        <linearGradient id="mid" href="#base"/>
        <linearGradient id="leaf" href="#mid" spreadMethod="repeat"/>
      </defs>
      <rect width="10" height="10" fill="url(#leaf)"/>
      <rect width="10" height="10" fill="url(#mid)"/>
      <rect width="10" height="10" fill="url(#base)"/>
    </svg>'''
    surface = surface_of(svg)
    originals = {
        name: (type(node), dict(node), len(node.children))
        for name, node in surface.gradients.items()}
    resolve_def_href(surface, 'leaf', surface.gradients)
    resolve_def_href(surface, 'mid', surface.gradients)
    resolve_def_href(surface, 'leaf', surface.gradients)
    for name, snapshot in originals.items():
        node = surface.gradients[name]
        assert (type(node), dict(node), len(node.children)) == snapshot


# --- File URL rendering (populates tree_cache) ---------------------------

CACHE_SVG_DEFS = '''
  <defs>
    <linearGradient id="base" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0" stop-color="red"/>
      <stop offset="1" stop-color="blue"/>
    </linearGradient>
    <linearGradient id="ovr" href="#base" x2="0" y2="1"/>
    <linearGradient id="chain" href="#ovr" spreadMethod="repeat"/>
  </defs>
'''


def _cache_document(shapes):
    return SVG_HEAD.format(w=120, h=40) + CACHE_SVG_DEFS + shapes + '</svg>'


CACHE_SHAPES = (
    '<g transform="translate(0,0)">'
    '<rect width="40" height="40" fill="url(#ovr)"/></g>'
    '<g transform="translate(40,0)">'
    '<circle cx="20" cy="20" r="20" fill="url(#chain)"/></g>'
    '<g transform="translate(80,0)">'
    '<rect width="40" height="40" fill="url(#base)"/></g>')

CACHE_SHAPES_REVERSED = (
    '<g transform="translate(80,0)">'
    '<rect width="40" height="40" fill="url(#base)"/></g>'
    '<g transform="translate(40,0)">'
    '<circle cx="20" cy="20" r="20" fill="url(#chain)"/></g>'
    '<g transform="translate(0,0)">'
    '<rect width="40" height="40" fill="url(#ovr)"/></g>')


def test_file_url_childless_href_keeps_inherited_stops(tmp_path):
    """Regression: tree_cache hits must honor parent_children.

    Rendering from a file URL populates ``tree_cache``; cache-hit Trees used
    to be rebuilt as plain Nodes without the parent_children flag, so
    childless referencing gradients lost their inherited stops and painted
    nothing.

    """
    svg_file = tmp_path / 'cached.svg'
    svg_file.write_text(_cache_document(CACHE_SHAPES))
    img = Image.open(io.BytesIO(svg2png(url=str(svg_file)))).convert('RGB')
    # The childless #chain circle inherits ovr's vertical vector and base's
    # stops: red at its top, blue at its bottom. Before the fix it was empty.
    assert img.getpixel((60, 4))[0] > 150
    assert img.getpixel((60, 36))[2] > 150
    # The #ovr rect (vertical vector override) is red at the top.
    assert img.getpixel((20, 2))[0] > 200


def test_file_url_order_swap_renders_identically(tmp_path):
    first = tmp_path / 'order-1.svg'
    second = tmp_path / 'order-2.svg'
    first.write_text(_cache_document(CACHE_SHAPES))
    second.write_text(_cache_document(CACHE_SHAPES_REVERSED))
    png_1 = Image.open(io.BytesIO(svg2png(url=str(first)))).convert('RGB')
    png_2 = Image.open(io.BytesIO(svg2png(url=str(second)))).convert('RGB')
    assert pixels(png_1) == pixels(png_2)


def test_file_url_and_bytestring_render_identically(tmp_path):
    svg = _cache_document(CACHE_SHAPES)
    svg_file = tmp_path / 'same.svg'
    svg_file.write_text(svg)
    from_file = Image.open(io.BytesIO(
        svg2png(url=str(svg_file)))).convert('RGB')
    from_bytes = Image.open(io.BytesIO(
        svg2png(bytestring=svg.encode()))).convert('RGB')
    assert pixels(from_file) == pixels(from_bytes)


def test_shipped_example_orders_render_identically(tmp_path):
    """The offline brand-icon examples must be order-independent on disk."""
    import os
    examples = os.path.join(
        os.path.dirname(__file__), os.pardir, 'docs', 'examples',
        'gradient-inheritance')
    order_a = os.path.join(examples, 'icons-order-a.svg')
    order_b = os.path.join(examples, 'icons-order-b.svg')
    if not os.path.exists(order_a):
        pytest.skip('shipped gradient examples are not present')
    png_a = Image.open(io.BytesIO(svg2png(url=order_a))).convert('RGB')
    png_b = Image.open(io.BytesIO(svg2png(url=order_b))).convert('RGB')
    assert pixels(png_a) == pixels(png_b)
    # All three icon regions actually contain painted pixels.
    rgba = Image.open(io.BytesIO(svg2png(url=order_a)))
    for x, y in ((36, 40), (120, 40), (196, 40)):
        assert rgba.getpixel((x, y))[3] > 0
