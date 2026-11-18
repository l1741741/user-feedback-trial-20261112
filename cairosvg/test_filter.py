"""
Pixel-level regression tests for SVG filters.

Only the filter primitives CairoSVG documents as supported are exercised:
``feOffset``, ``feFlood``, ``feBlend``, ``feComposite`` and
``feMerge``/``feMergeNode``. Fixtures are small, font-free and use plain
colors. Checked pixels are interior or exterior points picked with analytic
geometry, never antialiased edges, so exact RGBA comparisons are deterministic
across Cairo versions.

The expected positions were cross-checked against the filter rules of SVG 1.1
(§ 7.11, § 15.4, § 15.7) and Filter Effects 1 (§ 8, § 9), and against a
browser (headless Chromium) rendering the same fixtures.

Coordinates are device pixels of the output PNG: fixtures are rendered at
96 dpi with a 1:1 user-unit to pixel ratio and no viewBox scaling.

Colors:
    red    = (255, 0, 0)
    lime   = (0, 255, 0)
    blue   = (0, 0, 255)
    black  = (0, 0, 0)
    clear  = alpha 0

"""

import io
import pathlib

import pytest

from . import svg2png

RED = (255, 0, 0)
LIME = (0, 255, 0)
BLUE = (0, 0, 255)
BLACK = (0, 0, 0)


def render(svg_source, **options):
    """Render ``svg_source`` and return a Pillow RGBA image."""
    from PIL import Image
    png = svg2png(
        svg_source.encode() if isinstance(svg_source, str) else svg_source,
        **options)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def assert_clear(image, points):
    """Assert that each ``(x, y)`` probe is fully transparent."""
    for x, y in points:
        assert image.getpixel((x, y))[3] == 0, (
            f'pixel ({x}, {y}) = {image.getpixel((x, y))}, expected clear')


def opaque_bbox(image, color, tolerance=8):
    """Return the bounding box of opaque pixels close to ``color``."""
    width, height = image.size
    pixels = image.load()
    matches = [
        (x, y) for y in range(height) for x in range(width)
        if pixels[x, y][3] > 250 and all(
            abs(pixels[x, y][i] - color[i]) <= tolerance for i in range(3))]
    if not matches:
        return None
    xs, ys = zip(*matches)
    return min(xs), min(ys), max(xs), max(ys)


def document(body, defs='', width=200, height=200):
    """Wrap ``body`` in a plain document of ``width`` x ``height``."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}">'
        f'<defs>{defs}</defs>{body}</svg>')


SHADOW = (
    '<filter id="shadow" x="-30%" y="-30%" width="160%" height="160%">'
    '  <feOffset in="SourceAlpha" dx="20" dy="10" result="offset"/>'
    '  <feFlood flood-color="black" flood-opacity="0.5" result="flood"/>'
    '  <feComposite in="flood" in2="offset" operator="in" result="shadow"/>'
    '  <feMerge>'
    '    <feMergeNode in="shadow"/>'
    '    <feMergeNode in="SourceGraphic"/>'
    '  </feMerge>'
    '</filter>')

SHADOWED_RECT = (
    '<rect x="20" y="20" width="40" height="20" fill="red" '
    'filter="url(#shadow)"/>')


# ---------------------------------------------------------------------------
# feOffset: units, sign and the host transform
# ---------------------------------------------------------------------------


def test_offset_moves_only_the_filtered_shape():
    """feOffset shifts the filtered element in its own user space."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>'
        '<rect x="140" y="140" width="20" height="20" fill="lime"/>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>')
    image = render(svg)
    # Default region: -10%..110% of the 40x20 box, i.e. (16, 18) 48x24.
    # The (10, 5) offset is clipped to that region.
    assert opaque_bbox(image, RED) == (30, 25, 63, 41)
    # The unfiltered sibling does not move.
    assert opaque_bbox(image, LIME) == (140, 140, 159, 159)


def test_offset_follows_host_scale_once():
    """The host transform is applied once, not multiplied into the offset."""
    body = (
        '<g transform="scale(2)">'
        '  <rect x="20" y="20" width="40" height="20" fill="red" '
        '  filter="url(#f)"/>'
        '  <rect x="100" y="100" width="10" height="10" fill="lime"/>'
        '</g>')
    svg = document(
        body, defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>',
        width=300, height=300)
    image = render(svg)
    # Source: (40, 40) to (120, 80); offset (20, 10) in user space. The
    # region is scaled with the host transform: (32, 36) 96x48.
    assert opaque_bbox(image, RED) == (60, 50, 127, 83)
    # The sibling is drawn unshifted at (200, 200) to (220, 220).
    assert opaque_bbox(image, LIME) == (200, 200, 219, 219)


def test_offset_on_element_transform_matches_host_transform():
    """A filter on a transformed element uses the element's user space."""
    body = (
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)" transform="scale(2)"/>')
    svg = document(
        body, defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>')
    image = render(svg)
    assert opaque_bbox(image, RED) == (60, 50, 127, 83)


def test_negative_offset_moves_up_and_left():
    """A negative feOffset is honoured, not clamped to zero."""
    svg = document(
        '<rect x="40" y="40" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f"><feOffset dx="-20" dy="-10"/></filter>')
    image = render(svg)
    # Default region: (36, 38) 48x24.
    assert opaque_bbox(image, RED) == (36, 38, 59, 49)


def test_object_bounding_box_offset_is_per_axis():
    """objectBoundingBox offsets scale x and y by different box extents."""
    # A non-square 40x80 rectangle: 0.25 and 0.5 are 10 and 40 pixels.
    body = (
        '<rect x="20" y="20" width="40" height="80" fill="red" '
        'filter="url(#f)"/>')
    svg = document(
        body,
        defs='<filter id="f" x="-50%" y="-50%" width="200%" height="200%" '
             'primitiveUnits="objectBoundingBox">'
             '<feOffset dx="0.25" dy="0.5"/></filter>')
    image = render(svg)
    # Region -50%..150%: (-20, -40) 80x160, clipped by the 200x200 canvas.
    # The offset is +10 in x and +40 in y: the source box (20, 20) 40x80
    # becomes (30, 60) 40x80, so the result spans y = 60..139.
    assert opaque_bbox(image, RED) == (30, 60, 69, 139)


def test_object_bounding_box_negative_offset_is_per_axis():
    """Negative objectBoundingBox offsets keep their sign on both axes."""
    body = (
        '<rect x="60" y="40" width="40" height="80" fill="red" '
        'filter="url(#f)"/>')
    svg = document(
        body,
        defs='<filter id="f" x="-100%" y="-100%" width="300%" height="300%" '
             'primitiveUnits="objectBoundingBox">'
             '<feOffset dx="-0.25" dy="-0.5"/></filter>')
    image = render(svg)
    # -0.25 * 40 = -10 and -0.5 * 80 = -40.
    assert opaque_bbox(image, RED) == (50, 0, 89, 79)


def test_offset_uses_primitive_units_not_filter_units():
    """``filterUnits`` and ``primitiveUnits`` act on different attributes."""
    body = (
        '<rect x="20" y="20" width="40" height="80" fill="red" '
        'filter="url(#f)"/>')
    # The region uses user space; the offset uses box fractions.
    svg = document(
        body,
        defs='<filter id="f" filterUnits="userSpaceOnUse" '
             'x="0" y="0" width="200" height="200" '
             'primitiveUnits="objectBoundingBox">'
             '<feOffset dx="0.5" dy="0.5"/></filter>')
    image = render(svg)
    # +20 in x (half of 40) and +40 in y (half of 80).
    assert opaque_bbox(image, RED) == (40, 60, 79, 139)


def test_offset_inside_rotated_host_transform():
    """The offset is a user space vector and rotates with the element."""
    svg = document(
        '<rect x="0" y="0" width="40" height="20" fill="red" '
        'transform="translate(100, 100) rotate(90)" filter="url(#f)"/>',
        defs='<filter id="f" x="-200%" y="-200%" width="400%" height="400%">'
             '<feOffset dx="10" dy="0"/></filter>')
    image = render(svg)
    # After rotate(90), the local x axis points down: the 40x20 source
    # covers x = 80..99, y = 100..139, and the +10 x offset moves it down.
    assert opaque_bbox(image, RED) == (80, 110, 99, 149)


# ---------------------------------------------------------------------------
# Filter region and primitive subregion
# ---------------------------------------------------------------------------


def test_filter_region_is_clipped():
    """Pixels outside the declared filter region are not painted."""
    body = (
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>')
    # The region is exactly the source box, so the right and bottom parts
    # of the (10, 5) offset are clipped away.
    svg = document(
        body, defs='<filter id="f" x="0" y="0" width="100%" height="100%">'
        '<feOffset dx="10" dy="5"/></filter>')
    image = render(svg)
    assert opaque_bbox(image, RED) == (30, 25, 59, 39)
    assert_clear(image, [(25, 30), (55, 22), (65, 42)])


def test_user_space_filter_region_clips_in_user_units():
    """filterUnits=userSpaceOnUse uses user units, not box fractions."""
    body = (
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>')
    svg = document(
        body,
        defs='<filter id="f" filterUnits="userSpaceOnUse" '
             'x="25" y="25" width="20" height="10">'
             '<feOffset dx="10" dy="5"/></filter>')
    image = render(svg)
    assert opaque_bbox(image, RED) == (30, 25, 44, 34)


def test_user_space_filter_region_percentages_use_the_viewport():
    """Percentages of the region follow the normal SVG percentage rule."""
    svg = document(
        '<rect x="0" y="0" width="200" height="200" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f" filterUnits="userSpaceOnUse" '
             'x="10%" y="10%" width="50%" height="50%">'
             '<feFlood flood-color="blue"/></filter>')
    image = render(svg)
    # 10% and 50% of the 200x200 viewport: (20, 20) 100x100.
    assert opaque_bbox(image, BLUE) == (20, 20, 119, 119)


def test_default_region_object_bounding_box():
    """The default region is -10%..110% on both axes of the box."""
    svg = document(
        '<rect x="80" y="80" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f"><feFlood flood-color="blue"/></filter>')
    image = render(svg)
    assert opaque_bbox(image, BLUE) == (76, 78, 123, 101)


def test_default_region_user_space():
    """With userSpaceOnUse the default region covers the whole viewport."""
    svg = document(
        '<rect x="80" y="80" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f" filterUnits="userSpaceOnUse">'
             '<feFlood flood-color="blue"/></filter>')
    image = render(svg)
    assert opaque_bbox(image, BLUE) == (0, 0, 199, 199)


def test_primitive_subregion_clips_flood():
    """A primitive subregion clips its own result, not the host context."""
    body = (
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>')
    svg = document(
        body,
        defs='<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
             '<feFlood flood-color="blue" x="25" y="25" '
             'width="10" height="10"/></filter>')
    image = render(svg)
    # Absolute user space coordinates: (25, 25) 10x10.
    assert opaque_bbox(image, BLUE) == (25, 25, 34, 34)
    assert_clear(image, [(24, 24), (35, 25), (25, 35), (60, 60)])


def test_primitive_subregion_percentages_use_the_viewport():
    """Percentages of a user space subregion follow the viewport rule."""
    body = (
        '<rect x="0" y="0" width="200" height="200" fill="red" '
        'filter="url(#f)"/>')
    svg = document(
        body,
        defs='<filter id="f" filterUnits="userSpaceOnUse" '
             'x="50" y="50" width="100" height="100">'
             '<feFlood flood-color="blue" x="10%" y="10%" '
             'width="20%" height="20%"/></filter>')
    image = render(svg)
    # 10% of the 200-wide viewport is 20 and 20% is 40, so the subregion is
    # (20, 20) 40x40. Only the (50, 50) 100x100 filter region is rendered,
    # so the intersection (50, 50) 10x10 is what remains.
    assert opaque_bbox(image, BLUE) == (50, 50, 59, 59)


def test_object_bounding_box_subregion_is_per_axis():
    """objectBoundingBox subregions use the box width and height apart."""
    svg = document(
        '<rect x="20" y="20" width="40" height="80" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f" primitiveUnits="objectBoundingBox">'
             '<feFlood flood-color="blue" x="25%" y="25%" '
             'width="50%" height="50%"/></filter>')
    image = render(svg)
    # x = 20 + 10, y = 20 + 20, 20x40.
    assert opaque_bbox(image, BLUE) == (30, 40, 49, 79)


def test_object_bounding_box_subregion_accepts_fractions():
    """Bare numbers are fractions of the box, as in gradients."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f" primitiveUnits="objectBoundingBox">'
             '<feFlood flood-color="blue" x="0.25" y="0.25" '
             'width="0.5" height="0.5"/></filter>')
    image = render(svg)
    assert opaque_bbox(image, BLUE) == (30, 25, 49, 34)


def test_primitive_subregion_defaults_to_the_filter_region():
    """Without attributes, the subregion is the filter region."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
             '<feFlood flood-color="blue"/></filter>')
    image = render(svg)
    # Region: (0, 10) 80x40, clipped by the 200x200 canvas.
    assert opaque_bbox(image, BLUE) == (0, 10, 79, 49)


def test_primitive_with_empty_subregion_is_transparent():
    """A zero-width subregion disables the primitive, not the element."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>'
        '<rect x="140" y="140" width="20" height="20" fill="lime"/>',
        defs='<filter id="f" filterUnits="userSpaceOnUse" '
             'x="0" y="0" width="200" height="200">'
             '<feFlood flood-color="blue" x="0" y="0" '
             'width="0" height="20"/></filter>')
    image = render(svg)
    assert image.getchannel('A').getbbox() == (140, 140, 160, 160)
    assert opaque_bbox(image, LIME) == (140, 140, 159, 159)


def test_region_clips_the_result_not_the_source():
    """The filter region clips the primitive result, not its input image."""
    svg = document(
        '<rect x="0" y="0" width="50" height="50" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f" filterUnits="userSpaceOnUse" '
             'x="100" y="0" width="100" height="200">'
             '<feOffset dx="100" dy="0"/></filter>')
    image = render(svg)
    # The source is shifted into the region first: (100, 0) to (149, 49).
    assert opaque_bbox(image, RED) == (100, 0, 149, 49)


# ---------------------------------------------------------------------------
# Chains, inputs, results
# ---------------------------------------------------------------------------


def test_flood_does_not_leak_outside_region():
    """The unfiltered sibling stays exactly where it was drawn."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>'
        '<rect x="140" y="140" width="20" height="20" fill="lime"/>',
        defs='<filter id="f"><feFlood flood-color="blue"/></filter>')
    image = render(svg)
    # The flood covers the whole default region: (16, 18) 48x24 clipped by
    # the viewport, not the source box.
    assert opaque_bbox(image, BLUE) == (16, 18, 63, 41)
    assert opaque_bbox(image, LIME) == (140, 140, 159, 159)
    assert_clear(image, [(0, 0), (100, 100), (139, 139)])


def test_no_filter_sibling_is_painted_after_filtered_shape():
    """A later unfiltered sibling keeps its exact position and color."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>'
        '<rect x="20" y="100" width="40" height="20" fill="lime"/>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>')
    image = render(svg)
    assert opaque_bbox(image, RED) == (30, 25, 63, 41)
    assert opaque_bbox(image, LIME) == (20, 100, 59, 119)


def test_semi_transparent_shadow_keeps_its_alpha():
    """The shadow is semi-transparent and does not darken the source."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#shadow)"/>',
        defs=SHADOW)
    image = render(svg)
    # Source area is opaque red, shadow area is half-transparent black.
    assert image.getpixel((30, 30)) == (255, 0, 0, 255)
    assert image.getpixel((70, 40)) == (0, 0, 0, 128)
    # Undefined pixels left by the offset stay transparent.
    assert_clear(image, [(20, 45), (75, 30)])


def test_merge_order_paints_first_input_below():
    """feMergeNodes are painted in order: the last one is on top."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs=(
            '<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
            '  <feFlood flood-color="blue" result="blue"/>'
            '  <feMerge>'
            '    <feMergeNode in="SourceGraphic"/>'
            '    <feMergeNode in="blue"/>'
            '  </feMerge>'
            '</filter>'))
    image = render(svg)
    # SourceGraphic is merged first, so it is below the flood.
    assert image.getpixel((30, 30))[:3] == BLUE
    assert image.getpixel((10, 10))[:3] == BLUE


def test_blend_multiply_with_source_graphic():
    """feBlend combines its two inputs with the requested mode."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs=(
            '<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
            '  <feFlood flood-color="blue" result="blue"/>'
            '  <feBlend in="SourceGraphic" in2="blue" mode="multiply"/>'
            '</filter>'))
    image = render(svg)
    # multiply(red, blue) is black; the backdrop alone is blue.
    assert image.getpixel((30, 30))[:3] == BLACK
    assert image.getpixel((10, 10))[:3] == BLUE


@pytest.mark.parametrize('operator, expected', [
    ('in', (0, 0, 0, 255)),
    ('out', (0, 0, 0, 0)),
])
def test_composite_operators(operator, expected):
    """Supported feComposite operators follow Porter-Duff rules."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs=(
            '<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
            '  <feOffset in="SourceAlpha" dx="10" dy="5" result="offset"/>'
            '  <feFlood flood-color="black" result="flood"/>'
            f'  <feComposite in="flood" in2="offset" operator="{operator}"/>'
            '</filter>'))
    image = render(svg)
    # "in" keeps the overlap: black where the offset source is opaque.
    assert image.getpixel((40, 30)) == expected


def test_missing_in_uses_the_previous_result():
    """A primitive without ``in`` consumes the previous primitive's output."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs=(
            '<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
            '  <feFlood flood-color="blue" x="30" y="30" '
            '  width="10" height="10"/>'
            '  <feOffset dx="10" dy="5"/>'
            '</filter>'))
    image = render(svg)
    # The flood (30, 30) 10x10 is the implicit input, and the offset's
    # default subregion is the union of the referenced subregions, so the
    # shifted flood is clipped away: the result is empty, as for an offset
    # whose declared subregion does not keep its input.
    assert image.getchannel('A').getbbox() is None


def test_named_results_can_be_reused_and_reordered():
    """``result`` names let a primitive read any earlier output."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs=(
            '<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
            '  <feFlood flood-color="lime" x="10" y="10" '
            '  width="10" height="10" result="first"/>'
            '  <feFlood flood-color="blue" x="40" y="40" '
            '  width="10" height="10" result="second"/>'
            '  <feMerge>'
            '    <feMergeNode in="first"/>'
            '    <feMergeNode in="second"/>'
            '  </feMerge>'
            '</filter>'))
    image = render(svg)
    assert opaque_bbox(image, LIME) == (10, 10, 19, 19)
    assert opaque_bbox(image, BLUE) == (40, 40, 49, 49)


def test_forward_result_reference_behaves_as_missing():
    """A forward ``in`` reference falls back to the previous result."""
    svg = document(
        '<rect x="0" y="0" width="200" height="200" fill="red" '
        'filter="url(#f)"/>',
        defs=(
            '<filter id="f" filterUnits="userSpaceOnUse" '
            'x="0" y="0" width="200" height="200">'
            '  <feMerge><feMergeNode in="later"/></feMerge>'
            '  <feFlood flood-color="lime" x="40" y="40" '
            '  width="10" height="10" result="later"/>'
            '</filter>'))
    image = render(svg)
    # The merge is the first primitive: SourceGraphic, then the flood.
    # Nothing is painted outside the two, so the flood is the only color.
    assert opaque_bbox(image, LIME) == (40, 40, 49, 49)


# ---------------------------------------------------------------------------
# Multiple instances, nesting, ``use`` and repeated calls
# ---------------------------------------------------------------------------


def test_two_filtered_siblings_are_independent():
    """Each filtered element gets its own group and offset."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>'
        '<rect x="20" y="100" width="40" height="20" fill="lime" '
        'filter="url(#f)"/>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>')
    image = render(svg)
    assert opaque_bbox(image, RED) == (30, 25, 63, 41)
    assert opaque_bbox(image, LIME) == (30, 105, 63, 121)


def test_nested_filters_stack_offsets():
    """A filter applied to a group containing a filtered child composes."""
    svg = document(
        '<g filter="url(#f)">'
        '  <rect x="20" y="20" width="40" height="20" fill="red" '
        '  filter="url(#f)"/>'
        '</g>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>')
    image = render(svg)
    # Two nested offsets of (10, 5) compose: (20, 10) in total. Each filter
    # clips to its own region around the source box, (16, 18) 48x24.
    assert opaque_bbox(image, RED) == (40, 30, 63, 41)


def test_filtered_group_with_several_children():
    """A filter on a group filters the group as a whole."""
    svg = document(
        '<g filter="url(#f)">'
        '  <rect x="10" y="10" width="20" height="10" fill="red"/>'
        '  <rect x="50" y="30" width="20" height="10" fill="lime"/>'
        '</g>',
        defs='<filter id="f"><feFlood flood-color="blue"/></filter>')
    image = render(svg)
    # The box of the group is (10, 10) 60x30; the default region is
    # -10%..120% of it, i.e. (4, 7) 72x36.
    assert opaque_bbox(image, BLUE) == (4, 7, 75, 42)
    assert opaque_bbox(image, RED) is None
    assert opaque_bbox(image, LIME) is None


def test_use_element_with_filter_is_shifted_once():
    """A filtered shape reused with use keeps its own position."""
    svg = document(
        '<use href="#shape" x="100" filter="url(#f)"/>'
        '<rect x="160" y="140" width="20" height="20" fill="lime"/>',
        defs=(
            '<filter id="f"><feOffset dx="10" dy="5"/></filter>'
            '<rect id="shape" x="20" y="20" width="40" height="20" '
            'fill="red"/>'))
    image = render(svg)
    # The referenced box is (20, 20) 40x20, shifted to (120, 20); its
    # default region is (116, 18) 48x24 and the offset adds (10, 5). The
    # shifted shape is clipped by that region, hence the right/bottom edge.
    assert opaque_bbox(image, RED) == (130, 25, 163, 41)
    assert opaque_bbox(image, LIME) == (160, 140, 179, 159)


def test_group_opacity_and_filter_are_applied_once():
    """Group opacity is not multiplied into the paint a second time."""
    svg = document(
        '<g opacity="0.5">'
        '  <rect x="20" y="20" width="40" height="20" fill="red" '
        '  filter="url(#f)"/>'
        '</g>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>')
    image = render(svg)
    assert image.getpixel((40, 30)) == (255, 0, 0, 128)


def test_repeated_conversion_is_deterministic():
    """Rendering the same document twice gives the same bytes."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#shadow)"/>',
        defs=SHADOW)
    first = svg2png(svg.encode())
    second = svg2png(svg.encode())
    assert first == second
    # Rendering another document in between does not change the result.
    render(document('<rect x="0" y="0" width="10" height="10"/>',
                    defs=SHADOW))
    assert svg2png(svg.encode()) == first


def test_filter_element_is_not_drawn_and_filters_do_not_mutate_nodes():
    """A second render of a tree reuses the same nodes without drift."""
    source = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>')
    image = render(source)
    assert opaque_bbox(image, RED) == (30, 25, 63, 41)
    # In particular feOffset must not have been applied to the context:
    # a plain sibling drawn after it is still at its own place.
    source = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>'
        '<rect x="100" y="100" width="10" height="10" fill="lime"/>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>')
    image = render(source)
    assert opaque_bbox(image, LIME) == (100, 100, 109, 109)


# ---------------------------------------------------------------------------
# Documented fallbacks
# ---------------------------------------------------------------------------


def test_filter_with_href_inherits_primitives():
    """A filter referencing another one inherits its primitives."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#derived)"/>',
        defs=(
            '<filter id="base"><feOffset dx="10" dy="5"/></filter>'
            '<filter id="derived" href="#base"/>'))
    image = render(svg)
    assert opaque_bbox(image, RED) == (30, 25, 63, 41)


def test_filter_with_href_inherits_attributes():
    """Region attributes of a referenced filter are inherited."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#derived)"/>',
        defs=(
            '<filter id="base" x="0" y="0" width="100%" height="100%">'
            '<feOffset dx="10" dy="5"/></filter>'
            '<filter id="derived" href="#base"/>'))
    image = render(svg)
    # The inherited region is the source box: the offset is clipped away.
    assert opaque_bbox(image, RED) == (30, 25, 59, 39)


def test_empty_filter_draws_nothing():
    """An empty filter produces transparent black, as documented."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#empty)"/>'
        '<rect x="140" y="140" width="20" height="20" fill="lime"/>',
        defs='<filter id="empty"/>')
    image = render(svg)
    assert image.getchannel('A').getbbox() == (140, 140, 160, 160)
    assert opaque_bbox(image, LIME) == (140, 140, 159, 159)


def test_zero_sized_filter_region_disables_the_element():
    """A zero width or height region disables the filtered element."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#zero)"/>'
        '<rect x="140" y="140" width="20" height="20" fill="lime"/>',
        defs='<filter id="zero" width="0" height="0">'
             '<feOffset dx="10" dy="5"/></filter>')
    image = render(svg)
    assert image.getchannel('A').getbbox() == (140, 140, 160, 160)


def test_unsupported_filter_keeps_the_source_graphic():
    """A filter with only unsupported primitives leaves the graphic as is."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#blur)"/>'
        '<rect x="140" y="140" width="20" height="20" fill="lime"/>',
        defs='<filter id="blur"><feGaussianBlur stdDeviation="2"/></filter>')
    image = render(svg)
    assert opaque_bbox(image, RED) == (20, 20, 59, 39)
    assert opaque_bbox(image, LIME) == (140, 140, 159, 159)


def test_supported_primitives_around_unsupported_ones_still_apply():
    """Unsupported primitives are skipped, the supported chain keeps working.

    CairoSVG documents that only feOffset, feBlend, feFlood, feComposite and
    feMerge are supported. A blur in the middle of a chain is not substituted
    for anything: the next primitive uses the previous supported result, as
    for an unknown result.

    """
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs=(
            '<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
            '  <feOffset dx="10" dy="5" result="a"/>'
            '  <feGaussianBlur in="a" result="b"/>'
            '  <feOffset in="b" dx="3" dy="2"/>'
            '</filter>'))
    image = render(svg)
    # Two offsets compose, the blur is skipped: (13, 7) in total.
    assert opaque_bbox(image, RED) == (33, 27, 72, 46)


def test_missing_filter_keeps_the_source_graphic():
    """A non-existent filter reference is ignored, the graphic is kept."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#missing)"/>')
    image = render(svg)
    assert opaque_bbox(image, RED) == (20, 20, 59, 39)


def test_unsupported_standard_input_keeps_the_source_graphic():
    """BackgroundImage cannot be captured, so the whole filter is ignored."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs=(
            '<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
            '  <feBlend in="SourceGraphic" in2="BackgroundImage"/>'
            '</filter>'))
    image = render(svg)
    assert opaque_bbox(image, RED) == (20, 20, 59, 39)


def test_opacity_is_applied_once_with_filter():
    """Filtered shapes with opacity are not darkened twice."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'opacity="0.5" filter="url(#f)"/>',
        defs='<filter id="f"><feFlood flood-color="red"/></filter>')
    image = render(svg)
    assert image.getpixel((30, 30)) == (255, 0, 0, 128)


# ---------------------------------------------------------------------------
# Byte-exact regression images
# ---------------------------------------------------------------------------

REGRESSION_FOLDER = (
    pathlib.Path(__file__).resolve().parent.parent / 'regression_pngs')

# Fixtures whose reference render is stored in ``regression_pngs``. They are
# the same documents as the pixel tests above, kept here so both checks share
# one source.
REGRESSION_FIXTURES = {
    'test_offset_moves_only_the_filtered_shape': document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>'
        '<rect x="140" y="140" width="20" height="20" fill="lime"/>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>'),
    'test_offset_follows_host_scale_once': document(
        '<g transform="scale(2)">'
        '  <rect x="20" y="20" width="40" height="20" fill="red" '
        '  filter="url(#f)"/>'
        '  <rect x="100" y="100" width="10" height="10" fill="lime"/>'
        '</g>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>',
        width=300, height=300),
    'test_object_bounding_box_offset_is_per_axis': document(
        '<rect x="20" y="20" width="40" height="80" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f" x="-50%" y="-50%" width="200%" height="200%" '
             'primitiveUnits="objectBoundingBox">'
             '<feOffset dx="0.25" dy="0.5"/></filter>'),
    'test_filter_region_is_clipped': document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f" x="0" y="0" width="100%" height="100%">'
             '<feOffset dx="10" dy="5"/></filter>'),
    'test_region_clips_the_result_not_the_source': document(
        '<rect x="0" y="0" width="50" height="50" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f" filterUnits="userSpaceOnUse" '
             'x="100" y="0" width="100" height="200">'
             '<feOffset dx="100" dy="0"/></filter>'),
    'test_semi_transparent_shadow_keeps_its_alpha': document(
        SHADOWED_RECT, defs=SHADOW),
    'test_two_filtered_siblings_are_independent': document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>'
        '<rect x="20" y="100" width="40" height="20" fill="lime" '
        'filter="url(#f)"/>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>'),
    'test_nested_filters_stack_offsets': document(
        '<g filter="url(#f)">'
        '  <rect x="20" y="20" width="40" height="20" fill="red" '
        '  filter="url(#f)"/>'
        '</g>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>'),
    'test_no_filter_sibling_is_painted_after_filtered_shape': document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>'
        '<rect x="20" y="100" width="40" height="20" fill="lime"/>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>'),
    'test_use_element_with_filter_is_shifted_once': document(
        '<use href="#shape" x="100" filter="url(#f)"/>'
        '<rect x="160" y="140" width="20" height="20" fill="lime"/>',
        defs=(
            '<filter id="f"><feOffset dx="10" dy="5"/></filter>'
            '<rect id="shape" x="20" y="20" width="40" height="20" '
            'fill="red"/>')),
    'test_empty_filter_draws_nothing': document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#empty)"/>'
        '<rect x="140" y="140" width="20" height="20" fill="lime"/>',
        defs='<filter id="empty"/>'),
}


@pytest.mark.parametrize('name', sorted(REGRESSION_FIXTURES))
def test_filter_regression_pngs(name):
    """Renders are byte-identical to the stored reference images."""
    reference = REGRESSION_FOLDER / f'{name}.png'
    assert reference.exists(), f'missing reference image {reference}'
    assert svg2png(REGRESSION_FIXTURES[name].encode()) == (
        reference.read_bytes())
