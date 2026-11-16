"""
Tests for the ``fill-rule`` and ``clip-rule`` presentation properties.

These tests do not compare against a hand-drawn reference image. They render
small, analytically understood geometries and assert on the actual pixels of
the rasterized output, so a failure means the visible region (not just the
parsed attribute) is wrong.
"""

import io
from pathlib import Path

import pytest

from . import svg2pdf, svg2png, svg2ps, svg2svg

FIXTURES = Path(__file__).parent / 'fixtures'

# Two concentric squares spanning (10..90) and (30..70). Both contours run
# clockwise, so the default nonzero rule fills the center while evenodd
# punches a hole.
RINGS_CW = 'M10 10 H90 V90 H10 Z M30 30 H70 V70 H30 Z'
# Same geometry with the inner contour reversed.
RINGS_OPPOSITE = 'M10 10 H90 V90 H10 Z M30 30 V70 H70 V30 Z'
# Two overlapping circles with radius 25; the lens between them spans
# roughly x=45..55 and is winding number 2 when both circles agree.
TWO_CIRCLES_CW = (
    'M55 55 A25 25 0 1 1 5 55 A25 25 0 1 1 55 55 Z '
    'M95 55 A25 25 0 1 1 45 55 A25 25 0 1 1 95 55 Z')
TWO_CIRCLES_MIXED = (
    'M55 55 A25 25 0 1 1 5 55 A25 25 0 1 1 55 55 Z '
    'M95 55 A25 25 0 1 0 45 55 A25 25 0 1 0 95 55 Z')
# Single self-intersecting closed contour (bowtie), crossing at (50, 50).
BOWTIE = 'M15 20 L85 80 L85 20 L15 80 Z'
# Same ring geometry, but the contours are never explicitly closed.
UNCLOSED_RINGS = 'M10 10 H90 V90 H10 M30 30 H70 V70 H30'


def render_png(body, width=100, height=100):
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'{body}</svg>')
    return svg2png(svg.encode())


def pixels(png):
    from PIL import Image
    return Image.open(io.BytesIO(png)).convert('RGBA')


def is_empty(image, x, y):
    return image.getpixel((x, y))[3] == 0


def is_opaque(image, x, y):
    return image.getpixel((x, y))[3] == 255


def is_semi_transparent(image, x, y):
    alpha = image.getpixel((x, y))[3]
    return 0 < alpha < 255


# ---------------------------------------------------------------------------
# Ordinary paths: nonzero versus evenodd
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('d', (RINGS_CW, RINGS_OPPOSITE))
def test_evenodd_punches_hole_regardless_of_direction(d):
    image = pixels(render_png(
        f'<path d="{d}" fill="red" fill-rule="evenodd"/>'))
    assert is_opaque(image, 50, 15)   # outer ring band
    assert is_empty(image, 50, 50)    # hole


def test_nonzero_opposite_directions_make_a_hole():
    image = pixels(render_png(f'<path d="{RINGS_OPPOSITE}" fill="red"/>'))
    assert is_opaque(image, 50, 15)
    assert is_empty(image, 50, 50)


def test_nonzero_same_direction_fills_solid():
    image = pixels(render_png(f'<path d="{RINGS_CW}" fill="red"/>'))
    assert is_opaque(image, 50, 15)
    assert is_opaque(image, 50, 50)


def test_evenodd_hollows_overlap_of_two_subpaths():
    image = pixels(render_png(
        f'<path d="{TWO_CIRCLES_CW}" fill="red" fill-rule="evenodd"/>'))
    assert is_opaque(image, 22, 55)    # left circle body
    assert is_empty(image, 50, 55)     # shared lens


def test_nonzero_keeps_overlap_when_directions_agree():
    image = pixels(render_png(
        f'<path d="{TWO_CIRCLES_CW}" fill="red"/>'))
    assert is_opaque(image, 22, 55)
    assert is_opaque(image, 50, 55)


def test_nonzero_cancels_overlap_when_directions_oppose():
    image = pixels(render_png(
        f'<path d="{TWO_CIRCLES_MIXED}" fill="red"/>'))
    assert is_opaque(image, 22, 55)
    assert is_empty(image, 50, 55)


def test_self_intersecting_contour_paints_its_triangles():
    for rule in ('nonzero', 'evenodd'):
        image = pixels(render_png(
            f'<path d="{BOWTIE}" fill="red" fill-rule="{rule}"/>'))
        assert is_opaque(image, 30, 35)   # left triangle interior
        assert is_opaque(image, 65, 60)   # right triangle interior


def test_unclosed_subpaths_are_closed_implicitly():
    image = pixels(render_png(
        f'<path d="{UNCLOSED_RINGS}" fill="red" fill-rule="evenodd"/>'))
    assert is_opaque(image, 50, 15)
    assert is_empty(image, 50, 50)


def test_invalid_fill_rule_falls_back_to_nonzero():
    image = pixels(render_png(
        f'<path d="{RINGS_CW}" fill="red" fill-rule="mosaic"/>'))
    assert is_opaque(image, 50, 50)


# ---------------------------------------------------------------------------
# Inheritance, local overrides and rule isolation between operations
# ---------------------------------------------------------------------------

def test_fill_rule_inherited_from_group():
    body = (
        f'<g fill-rule="evenodd"><path d="{RINGS_CW}" fill="red"/></g>')
    image = pixels(render_png(body))
    assert is_empty(image, 50, 50)
    assert is_opaque(image, 50, 15)


def test_local_nonzero_overrides_inherited_evenodd():
    body = (
        '<g fill-rule="evenodd">'
        f'<path d="{RINGS_CW}" fill="red"/>'
        f'<path d="{RINGS_CW}" fill="blue" fill-rule="nonzero" '
        'transform="translate(0,0)"/>'
        '</g>')
    image = pixels(render_png(body))
    # Blue is painted last on the same geometry and is solid in the center.
    assert image.getpixel((50, 50))[:3] == (0, 0, 255)


def test_rule_does_not_leak_to_later_siblings():
    body = (
        f'<path d="{RINGS_CW}" fill="red" fill-rule="evenodd"/>'
        f'<path d="{RINGS_CW}" fill="green"/>')
    image = pixels(render_png(body))
    # Green has no fill-rule of its own and must use nonzero, filling solid.
    assert image.getpixel((50, 50))[:3] == (0, 128, 0)


@pytest.mark.parametrize('order', (0, 1))
def test_rule_isolation_with_order_swap(order):
    rect = '<rect x="0" y="0" width="20" height="100" fill="blue"/>'
    ring = f'<path d="{RINGS_CW}" fill="red" fill-rule="evenodd"/>'
    body = (rect + ring) if order == 0 else (ring + rect)
    image = pixels(render_png(body))
    assert is_opaque(image, 10, 50)    # plain rect unaffected
    assert is_empty(image, 50, 50)     # ring keeps its hole


def test_css_style_attribute_sets_fill_rule():
    body = (
        f'<path d="{RINGS_CW}" fill="red" '
        'style="fill-rule: evenodd"/>')
    image = pixels(render_png(body))
    assert is_empty(image, 50, 50)


def test_evenodd_inside_transparent_transformed_group():
    body = (
        '<g opacity="0.5" transform="translate(5,10)">'
        f'<path d="{RINGS_CW}" fill="blue" fill-rule="evenodd"/>'
        '</g>')
    image = pixels(render_png(body))
    assert is_semi_transparent(image, 55, 25)
    assert is_empty(image, 55, 60)


# ---------------------------------------------------------------------------
# clipPath: clip-rule and inherited fill-rule
# ---------------------------------------------------------------------------

def clipped_png(clip_body, rule=''):
    body = (
        '<defs><clipPath id="c"'
        f'{(" " + rule) if rule else ""}>{clip_body}</clipPath></defs>'
        '<rect width="100" height="100" fill="green" '
        'clip-path="url(#c)"/>')
    return pixels(render_png(body))


def test_clip_rule_evenodd_on_clip_child():
    image = clipped_png(
        f'<path d="{RINGS_CW}" clip-rule="evenodd"/>')
    assert is_opaque(image, 50, 15)
    assert is_empty(image, 50, 50)


def test_clip_rule_evenodd_on_clippath_element():
    image = clipped_png(f'<path d="{RINGS_CW}"/>',
                        rule='clip-rule="evenodd"')
    assert is_opaque(image, 50, 15)
    assert is_empty(image, 50, 50)


def test_fill_rule_on_clip_child_feeds_clip_rule():
    image = clipped_png(
        f'<path d="{RINGS_CW}" fill-rule="evenodd"/>')
    assert is_opaque(image, 50, 15)
    assert is_empty(image, 50, 50)


def test_clip_defaults_to_winding():
    image = clipped_png(f'<path d="{RINGS_CW}"/>')
    assert is_opaque(image, 50, 15)
    assert is_opaque(image, 50, 50)


def test_invalid_clip_rule_falls_back_to_winding():
    image = clipped_png(
        f'<path d="{RINGS_CW}" clip-rule="pancakes"/>')
    assert is_opaque(image, 50, 50)


def test_empty_clip_path_hides_everything():
    body = (
        '<defs><clipPath id="c"></clipPath></defs>'
        '<rect width="100" height="100" fill="red" clip-path="url(#c)"/>')
    image = pixels(render_png(body))
    assert is_empty(image, 50, 50)


def test_multi_child_clip_path_is_a_union():
    body = (
        '<defs><clipPath id="c">'
        '<rect x="10" y="10" width="30" height="30" clip-rule="evenodd"/>'
        '<rect x="60" y="60" width="30" height="30" clip-rule="evenodd"/>'
        '</clipPath></defs>'
        '<rect width="100" height="100" fill="red" clip-path="url(#c)"/>')
    image = pixels(render_png(body))
    assert is_opaque(image, 25, 25)
    assert is_opaque(image, 75, 75)
    assert is_empty(image, 50, 50)


def test_polygon_honors_inherited_evenodd():
    points = ('10,10 90,10 90,90 10,90 '
              '30,30 70,30 70,70 30,70')
    body = (
        f'<g fill-rule="evenodd"><polygon points="{points}" '
        'fill="red"/></g>')
    image = pixels(render_png(body))
    assert is_empty(image, 50, 50)
    assert is_opaque(image, 50, 15)


def test_css_stylesheet_sets_fill_rule():
    body = (
        '<style>path { fill-rule: evenodd; }</style>'
        f'<path d="{RINGS_CW}" fill="red"/>')
    image = pixels(render_png(body))
    assert is_empty(image, 50, 50)


def test_clip_rule_reused_through_use():
    body = (
        '<defs>'
        f'<path id="p" d="{RINGS_CW}" fill-rule="evenodd" '
        'clip-rule="evenodd"/>'
        '<clipPath id="c"><use href="#p"/></clipPath>'
        '</defs>'
        '<rect width="100" height="100" fill="green" '
        'clip-path="url(#c)"/>')
    image = pixels(render_png(body))
    assert is_opaque(image, 50, 15)
    assert is_empty(image, 50, 50)


def test_clip_does_not_reset_fill_rule_of_clipped_children():
    body = (
        '<defs><clipPath id="c">'
        '<rect x="0" y="0" width="100" height="100"/></clipPath></defs>'
        '<g clip-path="url(#c)" fill-rule="evenodd">'
        f'<path d="{RINGS_CW}" fill="red"/>'
        '</g>')
    image = pixels(render_png(body))
    assert is_empty(image, 50, 50)


def test_clip_with_object_bounding_box_units():
    body = (
        '<defs><clipPath id="c" clipPathUnits="objectBoundingBox">'
        '<path d="M0 0 H1 V1 H0 Z M0.3 0.3 H0.7 V0.7 H0.3 Z" '
        'clip-rule="evenodd"/></clipPath></defs>'
        '<rect x="0" y="0" width="100" height="100" fill="green" '
        'clip-path="url(#c)"/>')
    image = pixels(render_png(body))
    assert is_opaque(image, 50, 15)
    assert is_empty(image, 50, 50)


# ---------------------------------------------------------------------------
# Backend coverage and the rendered legend fixture
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('converter', (svg2png, svg2pdf, svg2ps, svg2svg))
def test_clip_rule_supported_on_every_backend(converter):
    body = (
        '<defs><clipPath id="c">'
        f'<path d="{RINGS_CW}" clip-rule="evenodd"/></clipPath></defs>'
        '<rect width="100" height="100" fill="green" '
        'clip-path="url(#c)"/>')
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
        f'viewBox="0 0 100 100">{body}</svg>').encode()
    out = converter(svg)
    assert out
    if converter is svg2png:
        image = pixels(out)
        assert is_empty(image, 50, 50)
        assert is_opaque(image, 50, 15)


def test_legend_fixture_renders_expected_regions():
    png = svg2png(url=str(FIXTURES / 'fill_rule_legend.svg'))
    image = pixels(png)

    def empty(col, row, x, y):
        return is_empty(image, col * 100 + x, row * 100 + y)

    def opaque(col, row, x, y):
        return is_opaque(image, col * 100 + x, row * 100 + y)

    # Row 1: nonzero solid, evenodd hole, hollow lens, bowtie triangles.
    assert opaque(0, 0, 50, 50)
    assert empty(1, 0, 50, 50) and opaque(1, 0, 50, 15)
    assert empty(2, 0, 50, 55) and opaque(2, 0, 22, 55)
    assert opaque(3, 0, 30, 35) and opaque(3, 0, 65, 60)
    # Row 2: evenodd clips (child/parent), winding clip, reused clip.
    assert empty(0, 1, 50, 50) and opaque(0, 1, 50, 15)
    assert empty(1, 1, 50, 50)
    assert opaque(2, 1, 50, 50)
    assert empty(3, 1, 50, 50) and opaque(3, 1, 50, 15)
    # Row 3: inherited hole, override solid, semi-transparent hole,
    # clipped group whose yellow ring survives with its hole.
    assert empty(0, 2, 50, 50)
    assert opaque(1, 2, 50, 50)
    assert is_semi_transparent(image, 260, 225) and empty(2, 2, 55, 60)
    assert empty(3, 2, 50, 50)
    assert image.getpixel((350, 215))[:3] == (255, 255, 0)
