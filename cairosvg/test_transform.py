"""
Tests for ``transform-origin``, ``transform-box`` and transform composition.

The tests render small deterministic SVG documents and locate the colored
geometry in device pixels, so that every claim about a rotation center or a
corner position can be checked against an actual raster result instead of
against internal matrix bookkeeping.

"""

import io

import pytest

from . import svg2png
from .helpers import (
    TransformOriginError, _looks_like_length, resolve_transform_origin,
    transform)
from .parser import Tree
from .surface import PNGSurface

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

requires_pillow = pytest.mark.skipif(
    Image is None, reason='Pillow is required for pixel-level assertions')


def render(svg_body, width=200, height=200, view_box=None,
           extra_svg_attrs=''):
    view_box_attr = f' viewBox="{view_box}"' if view_box else ''
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}"{view_box_attr} {extra_svg_attrs}>'
        f'{svg_body}</svg>')
    return svg2png(svg.encode())


def colored_pixels(png, width=200, height=200, channel='red'):
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    pixels = image.load()
    match = []
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels[x, y]
            if not a:
                continue
            if channel == 'red' and r > 150 and g < 80 and b < 80:
                match.append((x, y))
            elif channel == 'blue' and b > 150 and r < 80 and g < 80:
                match.append((x, y))
    return match


def pixel_center(pixels):
    count = len(pixels)
    return (
        sum(x for x, _ in pixels) / count,
        sum(y for _, y in pixels) / count)


def pixel_bbox(pixels):
    xs = [x for x, _ in pixels]
    ys = [y for _, y in pixels]
    return min(xs), min(ys), max(xs), max(ys)


def surface_for(svg):
    tree = Tree(bytestring=svg.encode())
    return PNGSurface(tree, None, 96), tree


# ---------------------------------------------------------------------------
# transform-origin: explicit lengths and keywords
# ---------------------------------------------------------------------------

@requires_pillow
def test_explicit_length_origin_keeps_rotation_center_fixed():
    """A 90deg rotation about the rect center leaves the center in place."""
    png = render(
        '<rect x="70" y="90" width="60" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="100 100"/>')
    pixels = colored_pixels(png)
    cx, cy = pixel_center(pixels)
    assert (cx, cy) == pytest.approx((99.5, 99.5), abs=0.6)
    # The 60x20 rect becomes a 20x60 rect around the same center.
    assert pixel_bbox(pixels) == (90, 70, 109, 129)


@requires_pillow
def test_keyword_origin_resolves_in_local_user_space():
    """``center`` must reference the local viewport, not device pixels."""
    # ViewBox maps 100 user units onto 200 device pixels: local center
    # (50, 50) lands on device (100, 100).
    png = render(
        '<rect x="35" y="45" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="center"/>',
        view_box='0 0 100 100')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((100, 100), abs=1)


@requires_pillow
def test_keyword_origin_with_shifted_viewbox():
    """ViewBox min-x/min-y participate in the reference box."""
    png = render(
        '<rect x="-15" y="-5" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="center"/>',
        view_box='-50 -50 100 100')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((100, 100), abs=1)


@requires_pillow
def test_percentage_origin_under_viewbox_scale():
    png = render(
        '<rect x="35" y="45" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="50% 50%"/>',
        view_box='0 0 100 100')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((100, 100), abs=1)


@requires_pillow
def test_percentage_origin_in_nested_translated_group():
    """The origin is expressed in the group's local coordinate system."""
    # Local viewport center (50, 50); rect center local (25, 25). Rotated
    # about (50, 50) it reaches local (75, 25); group offset (25, 25) then
    # gives user (100, 50), device (200, 100) under the 2x viewBox scale.
    # The 20-device-wide square is centered on x=200, so its right half is
    # clipped by the canvas edge while its full vertical extent remains.
    png = render(
        '<g transform="translate(25,25)">'
        '<rect x="10" y="20" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="center"/></g>',
        view_box='0 0 100 100')
    pixels = colored_pixels(png)
    cx, cy = pixel_center(pixels)
    assert cy == pytest.approx(100, abs=1)
    assert 190 <= cx <= 200
    assert pixel_bbox(pixels) == (190, 70, 199, 129)


@requires_pillow
def test_physical_unit_origin():
    """1in resolves through the DPI setting (96px at 96dpi)."""
    png = render(
        '<rect x="66" y="86" width="60" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="1in 1in"/>')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((95.5, 95.5), abs=1)


@requires_pillow
def test_comma_separated_origin():
    png_comma = render(
        '<rect x="70" y="90" width="60" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="100, 100"/>')
    png_space = render(
        '<rect x="70" y="90" width="60" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="100 100"/>')
    assert png_comma == png_space


@requires_pillow
def test_em_unit_origin():
    """5em at the default 16px font size resolves to 80 local units."""
    png = render(
        '<rect x="50" y="70" width="60" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="5em 5em"/>')
    cx, cy = pixel_center(colored_pixels(png))
    # Rect center (80, 80) stays fixed under the rotation.
    assert (cx, cy) == pytest.approx((79.5, 79.5), abs=1)


@requires_pillow
def test_single_vertical_keyword_centers_horizontal_axis():
    # transform-origin="top" => x = viewport center (100), y = 0.
    png = render(
        '<rect x="70" y="0" width="60" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="top"/>')
    pixels = colored_pixels(png)
    # Rect center (100, 10), rotated about (100, 0) -> (90, 0).
    cx, cy = pixel_center(pixels)
    assert (cx, cy) == pytest.approx((89.5, 14.5), abs=1)
    assert pixel_bbox(pixels) == (80, 0, 99, 29)


@requires_pillow
def test_swapped_keyword_order():
    """``left top`` (vertical-style keyword first) resolves like ``0 0``."""
    png_keyword = render(
        '<rect x="70" y="90" width="60" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="left top"/>')
    png_numeric = render(
        '<rect x="70" y="90" width="60" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="0 0"/>')
    assert png_keyword == png_numeric


# ---------------------------------------------------------------------------
# transform-box: fill-box
# ---------------------------------------------------------------------------

@requires_pillow
def test_fill_box_uses_object_bounding_box():
    """An off-center rect rotates about its own geometry center."""
    png = render(
        '<rect x="60" y="80" width="40" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="center" '
        'transform-box="fill-box"/>')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((79.5, 89.5), abs=1)
    assert pixel_bbox(colored_pixels(png)) == (70, 70, 89, 109)


@requires_pillow
def test_fill_box_zero_origin_is_bounding_box_corner():
    # Origin (0, 0) with fill-box == top-left of the bounding box (60, 80);
    # the rect center (80, 90) rotates to (70, 100).
    png = render(
        '<rect x="60" y="80" width="40" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="0 0" '
        'transform-box="fill-box"/>')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((49.5, 99.5), abs=1)


@requires_pillow
def test_fill_box_keyword_pair():
    # "center top" with fill-box: origin (80, 80).
    png = render(
        '<rect x="60" y="80" width="40" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="center top" '
        'transform-box="fill-box"/>')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((69.5, 79.5), abs=1)


@requires_pillow
def test_degenerate_fill_box_falls_back_safely():
    """A zero-width rect must not crash and must not hide later nodes."""
    png = render(
        '<rect x="60" y="80" width="0" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="center" '
        'transform-box="fill-box"/>'
        '<rect x="100" y="100" width="10" height="10" fill="red"/>')
    pixels = colored_pixels(png)
    # Only the second rect renders: exactly a 10x10 device square.
    assert pixel_bbox(pixels) == (100, 100, 109, 109)


@requires_pillow
def test_unknown_transform_box_falls_back_to_view_box():
    png = render(
        '<rect x="70" y="90" width="60" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="center" '
        'transform-box="border-box"/>')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((99.5, 99.5), abs=1)


@requires_pillow
def test_fill_box_on_text_matches_explicit_origin():
    """Text geometry has no box until layout runs; it must still resolve."""
    fill_box_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<text x="50" y="100" font-size="30" fill="red" '
        'transform="rotate(180)" transform-origin="center" '
        'transform-box="fill-box">Hi</text></svg>')
    # The same rotation about the measured bounding-box center expressed as
    # explicit user-space coordinates must produce identical pixels.
    explicit_svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<text x="50" y="100" font-size="30" fill="red" '
        'transform="rotate(180)" '
        'transform-origin="82.49755859375 110.75927734375">Hi</text></svg>')
    assert svg2png(fill_box_svg.encode()) == svg2png(explicit_svg.encode())
    # And the document actually contains painted glyphs.
    assert colored_pixels(svg2png(fill_box_svg.encode()))


@requires_pillow
def test_fill_box_on_circle_keeps_center_fixed():
    png = render(
        '<circle cx="100" cy="100" r="30" fill="red" '
        'transform="rotate(90)" transform-origin="center" '
        'transform-box="fill-box"/>')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((99.5, 99.5), abs=1)


@requires_pillow
def test_fill_box_percentage_origin():
    # Rect (40,60) 80x40: 25% 25% of its box is (60,70); the rect center
    # (80,80) rotates about that point to device (50,90).
    png = render(
        '<rect x="40" y="60" width="80" height="40" fill="red" '
        'transform="rotate(90)" transform-origin="25% 25%" '
        'transform-box="fill-box"/>')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((49.5, 89.5), abs=1)


# ---------------------------------------------------------------------------
# Malformed values must not poison the following nodes
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('bad_origin', [
    'garbage', '', '1 2 3', 'foo bar', '10px garbage',
])
def test_invalid_origin_is_ignored_not_raised(bad_origin):
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        f'<rect x="10" y="10" width="20" height="20" fill="red" '
        f'transform="rotate(90)" transform-origin="{bad_origin}"/>'
        '<rect x="50" y="50" width="20" height="20" fill="red"/></svg>')
    # The document renders without raising.
    png = svg2png(svg.encode())
    pixels = colored_pixels(png)
    # The second rect is always drawn at the same place.
    second = [p for p in pixels if p[0] >= 50]
    assert pixel_bbox(second) == (50, 50, 69, 69)


def test_malformed_transform_functions_are_skipped():
    """A broken transform entry leaves the context usable for siblings."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<rect x="0" y="0" width="10" height="10" fill="red" '
        'transform="bogus(1 2) translate(100, 100)"/>'
        '<rect x="50" y="50" width="10" height="10" fill="red"/></svg>')
    png = svg2png(svg.encode())
    pixels = colored_pixels(png)
    # Second rect untouched at (50, 50); first one still got its translate.
    second = [p for p in pixels if 50 <= p[0] < 60]
    first = [p for p in pixels if p[0] >= 100]
    assert pixel_bbox(second) == (50, 50, 59, 59)
    assert pixel_bbox(first) == (100, 100, 109, 109)


def test_resolve_origin_returns_none_on_bad_input():
    surface, tree = surface_for(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<rect id="r" width="10" height="10"/></svg>')
    rect = tree.children[0]
    rect['transform-origin'] = 'not-a-length'
    assert resolve_transform_origin(surface, rect) is None
    rect['transform-origin'] = '1 2 3'
    assert resolve_transform_origin(surface, rect) is None


def test_looks_like_length():
    assert _looks_like_length('10')
    assert _looks_like_length('-2.5px')
    assert _looks_like_length('.5in')
    assert _looks_like_length('50%')
    assert _looks_like_length('1e3em')
    assert not _looks_like_length('garbage')
    assert not _looks_like_length('10pixels')


# ---------------------------------------------------------------------------
# Composition order and nested groups
# ---------------------------------------------------------------------------

@requires_pillow
def test_flat_transform_list_order_matches_spec():
    """translate(100,100) rotate(90): rotate acts first on the geometry."""
    png = render(
        '<rect x="10" y="0" width="5" height="5" fill="red" '
        'transform="translate(100,100) rotate(90)"/>')
    # Local corner (10, 0)-(15, 5) rotates to (-5, 10)-(0, 15), then the
    # translate gives device (95, 110)-(100, 115).
    assert pixel_bbox(colored_pixels(png)) == (95, 110, 99, 114)


@requires_pillow
def test_nested_groups_match_flat_list():
    """Two nested groups must equal the equivalent flat transform list."""
    nested = render(
        '<g transform="translate(100,100)"><g transform="rotate(90)">'
        '<rect x="10" y="0" width="5" height="5" fill="red"/></g></g>')
    flat = render(
        '<rect x="10" y="0" width="5" height="5" fill="red" '
        'transform="translate(100,100) rotate(90)"/>')
    assert nested == flat


@requires_pillow
def test_nested_group_and_node_transform_compose():
    """The node's own transform combines with its parent group transform."""
    # Outer translate (40, 60); inner rect rotate 90 about explicit local
    # origin (20, 20). Rect (10, 10) 20x20 stays centered on local (20, 20),
    # so it lands on device (60, 80).
    png = render(
        '<g transform="translate(40,60)">'
        '<rect x="10" y="10" width="20" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="20 20"/></g>')
    cx, cy = pixel_center(colored_pixels(png))
    assert (cx, cy) == pytest.approx((59.5, 79.5), abs=1)


@requires_pillow
def test_three_level_nesting():
    """Each nesting level applies outside the previous one."""
    # translate(100,0) -> translate(0,100) -> rotate(90); rect at (10,0)
    # 10x10: rotate gives (0,10)-(−10,20); translates -> (100,110)-(90,120).
    png = render(
        '<g transform="translate(100,0)"><g transform="translate(0,100)">'
        '<g transform="rotate(90)">'
        '<rect x="10" y="0" width="10" height="10" fill="red"/>'
        '</g></g></g>')
    assert pixel_bbox(colored_pixels(png)) == (90, 110, 99, 119)


@requires_pillow
def test_scale_then_translate_order_in_list():
    # translate(50,50) scale(2): scale acts first on the 10x10 rect at (5,5)
    # -> local (10,10) 20x20, then translate -> (60,60) 20x20.
    png = render(
        '<rect x="5" y="5" width="10" height="10" fill="red" '
        'transform="translate(50,50) scale(2)"/>')
    assert pixel_bbox(colored_pixels(png)) == (60, 60, 79, 79)


# ---------------------------------------------------------------------------
# Node reuse: repeated rendering must not depend on previous state
# ---------------------------------------------------------------------------

def test_use_node_is_not_mutated_by_rendering():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100">'
        '<defs><rect id="r" width="20" height="20" fill="red"/></defs>'
        '<use href="#r" x="20" y="40"/>'
        '<use href="#r" x="120" y="40"/></svg>')
    tree = Tree(bytestring=svg.encode())
    use_x_before = [child.get('x') for child in tree.children
                    if child.tag == 'use']
    PNGSurface(tree, None, 96)
    use_x_after = [child.get('x') for child in tree.children
                   if child.tag == 'use']
    assert use_x_before == use_x_after == ['20', '120']


def test_same_tree_renders_identically_twice():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100">'
        '<defs><rect id="r" width="20" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="10 10"/></defs>'
        '<use href="#r" x="20" y="40"/>'
        '<use href="#r" x="120" y="40"/></svg>')
    tree = Tree(bytestring=svg.encode())
    first = io.BytesIO()
    PNGSurface(tree, first, 96)
    second = io.BytesIO()
    PNGSurface(tree, second, 96)
    assert first.getvalue() == second.getvalue()


@requires_pillow
def test_repeated_use_instances_keep_their_own_position():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100">'
        '<defs><rect id="r" width="20" height="20" fill="red"/></defs>'
        '<use href="#r" x="20" y="40"/>'
        '<use href="#r" x="120" y="40"/>'
        '<use href="#r" x="220" y="40"/></svg>')
    pixels = colored_pixels(svg2png(svg.encode()), width=300, height=100)
    for expected_x in (20, 120, 220):
        column = [p for p in pixels
                  if expected_x <= p[0] < expected_x + 20]
        assert pixel_bbox(column) == (
            expected_x, 40, expected_x + 19, 59)


@requires_pillow
def test_use_with_transform_origin_is_stable_across_instances():
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="200" '
        'viewBox="0 0 300 200">'
        '<defs><g id="g"><rect x="0" y="0" width="40" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="20 10"/></g></defs>'
        '<use href="#g" x="50" y="50"/><use href="#g" x="150" y="100"/></svg>')
    pixels = colored_pixels(
        svg2png(svg.encode()), width=300, height=200)
    first = [p for p in pixels if p[0] < 100]
    second = [p for p in pixels if p[0] >= 150]
    # Both instances show the same 20x40 rotated footprint.
    assert pixel_bbox(first) == (60, 40, 79, 79)
    assert pixel_bbox(second) == (160, 90, 179, 129)


# ---------------------------------------------------------------------------
# Default behavior regressions
# ---------------------------------------------------------------------------

@requires_pillow
def test_nested_svg_viewport_origin_restores_for_siblings():
    """After a nested <svg> with its own viewBox, later siblings must again
    resolve origins against the outer viewport."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<svg x="0" y="0" width="100" height="100" viewBox="-50 -50 100 100">'
        '<rect x="-15" y="-5" width="30" height="10" fill="red" '
        'transform="rotate(90)" transform-origin="center"/></svg>'
        '<rect x="70" y="150" width="60" height="20" fill="red" '
        'transform="rotate(90)" transform-origin="100 160"/></svg>')
    png = svg2png(svg.encode())
    pixels = colored_pixels(png)
    # The outer rect rotates about its own center (100, 160) and stays there.
    lower = [p for p in pixels if p[1] >= 130]
    cx, cy = pixel_center(lower)
    assert (cx, cy) == pytest.approx((99.5, 159.5), abs=1)
    # The inner rect is centered on the inner viewport center (50, 50).
    upper = [p for p in pixels if p[1] < 100]
    cx, cy = pixel_center(upper)
    assert (cx, cy) == pytest.approx((49.5, 49.5), abs=1)


@requires_pillow
def test_no_origin_defaults_to_svg_origin():
    """Without transform-origin, rotation stays about local (0, 0)."""
    png = render(
        '<rect x="100" y="100" width="20" height="10" fill="red" '
        'transform="rotate(90)"/>')
    # Rect center (110, 105) rotated about origin -> (-105, 110): off-canvas,
    # proving no implicit 50% 50% default was introduced.
    assert colored_pixels(png) == []


@requires_pillow
def test_plain_transform_without_origin_unchanged():
    png = render(
        '<g transform="translate(30,40)">'
        '<rect x="0" y="0" width="10" height="10" fill="red"/></g>')
    assert pixel_bbox(colored_pixels(png)) == (30, 40, 39, 49)


def test_transform_helper_without_string_is_noop(dummy_surface):
    # Must return early without touching the context.
    assert transform(dummy_surface, '') is None
    assert transform(dummy_surface, None) is None


def test_transform_origin_error_is_an_exception():
    assert issubclass(TransformOriginError, Exception)


@pytest.fixture
def dummy_surface():
    import cairocffi as cairo
    cairo_surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, 10, 10)

    class Dummy:
        context = cairo.Context(cairo_surface)
        context_width = 10
        context_height = 10
        dpi = 96
        font_size = 12
        viewport_origin = (0, 0)

    return Dummy()
