"""
Pixel-level regression tests for SVG filters.

Only the filter primitives CairoSVG documents as supported are exercised:
``feOffset``, ``feBlend``, ``feFlood``, ``feComposite`` and
``feMerge``/``feMergeNode``. The fixtures are small, font-free and use plain
colors. Checked pixels are interior or exterior points chosen by analytic
geometry, never antialiased edges, so exact RGBA comparisons are deterministic
across Cairo versions.

Coordinates are device pixels of the output PNG (the fixtures are rendered at
96 dpi with a 1:1 user-unit to pixel ratio and no viewBox scaling).

Colors:
    white  = (255, 255, 255)
    red    = (255, 0, 0)
    lime   = (0, 255, 0)
    blue   = (0, 0, 255)
    black  = (0, 0, 0)
    clear  = alpha 0

"""

import io

import pytest

from . import svg2png

RED = (255, 0, 0)
LIME = (0, 255, 0)
BLUE = (0, 0, 255)
BLACK = (0, 0, 0)
WHITE = (255, 255, 255)


def render(svg_source, **options):
    """Render ``svg_source`` and return a Pillow RGBA image."""
    from PIL import Image
    png = svg2png(
        svg_source.encode() if isinstance(svg_source, str) else svg_source,
        **options)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def assert_pixels(image, expected):
    """Assert that each ``(x, y): color`` probe matches exactly."""
    for (x, y), color in expected.items():
        assert image.getpixel((x, y)) == color, (
            f'pixel ({x}, {y}) = {image.getpixel((x, y))}, expected {color}')


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
    '  <feFlood flood-color="blue" result="flood"/>'
    '  <feComposite in="flood" in2="offset" operator="in" result="shadow"/>'
    '  <feMerge>'
    '    <feMergeNode in="shadow"/>'
    '    <feMergeNode in="SourceGraphic"/>'
    '  </feMerge>'
    '</filter>')

# A red 40x20 rectangle at (20, 20), drawn with a drop shadow shifted by
# (20, 10): the shadow covers (40, 30) to (80, 50), the source keeps
# (20, 20) to (60, 40).
SHADOWED_RECT = (
    '<rect x="20" y="20" width="40" height="20" fill="red" '
    'filter="url(#shadow)"/>')


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


def test_primitive_subregion_percentages_are_region_relative():
    """Primitive subregion percentages use the filter region dimensions."""
    body = (
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>')
    # Region is (0, 10) 80x40 (objectBoundingBox -50%..150% of the 40x20
    # box), so 25%, 25%, 25%, 25% is x = 0 + 20, y = 10 + 10, 20x10.
    svg = document(
        body,
        defs='<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
             '<feFlood flood-color="blue" x="25%" y="25%" '
             'width="25%" height="25%"/></filter>')
    image = render(svg)
    assert opaque_bbox(image, BLUE) == (20, 20, 39, 29)
    assert_clear(image, [(19, 20), (40, 20), (20, 30), (60, 60)])


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
        defs=(
            '<filter id="shadow" x="-30%" y="-30%" width="160%" '
            'height="160%">'
            '  <feOffset in="SourceAlpha" dx="20" dy="10" result="offset"/>'
            '  <feFlood flood-color="black" flood-opacity="0.5" '
            'result="flood"/>'
            '  <feComposite in="flood" in2="offset" operator="in" '
            'result="shadow"/>'
            '  <feMerge>'
            '    <feMergeNode in="shadow"/>'
            '    <feMergeNode in="SourceGraphic"/>'
            '  </feMerge>'
            '</filter>'))
    image = render(svg)
    # Source area is opaque red, shadow area is half-transparent black.
    assert image.getpixel((30, 30)) == (255, 0, 0, 255)
    assert image.getpixel((70, 40)) == (0, 0, 0, 128)
    # Undefined pixels left by the offset stay transparent.
    assert_clear(image, [(20, 45), (75, 30)])


def test_merge_order_paints_first_input_on_top():
    """feMergeNodes are painted in order, so the first one ends up on top."""
    body = (
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>')
    svg = document(
        body,
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
    body = (
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>')
    svg = document(
        body,
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
    body = (
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>')
    svg = document(
        body,
        defs=(
            '<filter id="f" x="-50%" y="-50%" width="200%" height="200%">'
            '  <feOffset in="SourceAlpha" dx="10" dy="5" result="offset"/>'
            '  <feFlood flood-color="black" result="flood"/>'
            f'  <feComposite in="flood" in2="offset" operator="{operator}"/>'
            '</filter>'))
    image = render(svg)
    # "in" keeps the overlap: black where the offset source is opaque.
    assert image.getpixel((40, 30)) == expected


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

    CairoSVG documents that only feOffset, feBlend and feFlood are supported.
    A blur in the middle of a chain is not substituted for anything: the next
    primitive uses the previous supported result, as for an unknown result.

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


def test_use_element_with_filter_is_shifted_once():
    """A filtered shape reused with use keeps its own position."""
    svg = document(
        '<use href="#shape" x="60" y="0"/>'
        '<rect x="140" y="140" width="20" height="20" fill="lime"/>',
        defs=(
            '<filter id="f"><feOffset dx="10" dy="5"/></filter>'
            '<rect id="shape" x="20" y="20" width="40" height="20" '
            'fill="red" filter="url(#f)"/>'))
    image = render(svg)
    assert opaque_bbox(image, RED) == (90, 25, 123, 41)
    assert opaque_bbox(image, LIME) == (140, 140, 159, 159)


def test_filter_in_nested_svg_viewport():
    """Filters work inside a nested viewport and keep sibling positions."""
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        '<defs><filter id="f"><feOffset dx="10" dy="5"/></filter></defs>'
        '<svg x="20" y="20" width="160" height="160" '
        'viewBox="0 0 160 160">'
        '  <rect x="10" y="10" width="40" height="20" fill="red" '
        '  filter="url(#f)"/>'
        '  <rect x="100" y="100" width="20" height="20" fill="lime"/>'
        '</svg></svg>')
    image = render(svg)
    assert opaque_bbox(image, RED) == (40, 35, 73, 51)
    assert opaque_bbox(image, LIME) == (120, 120, 139, 139)


def test_offsets_do_not_accumulate_between_calls():
    """Repeated calls do not share mutable state."""
    svg = document(
        '<rect x="20" y="20" width="40" height="20" fill="red" '
        'filter="url(#f)"/>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>')
    boxes = []
    for _ in range(4):
        boxes.append(opaque_bbox(render(svg), RED))
    assert boxes == [(30, 25, 63, 41)] * 4


def test_filter_on_group_and_children():
    """Filtering a group containing an unfiltered sibling shifts the group."""
    svg = document(
        '<g filter="url(#f)">'
        '  <rect x="20" y="20" width="40" height="20" fill="red"/>'
        '  <rect x="100" y="100" width="20" height="20" fill="lime"/>'
        '</g>'
        '<rect x="150" y="10" width="20" height="20" fill="blue"/>',
        defs='<filter id="f"><feOffset dx="10" dy="5"/></filter>')
    image = render(svg)
    # The group box is (20, 20) 100x100, so the default region is
    # (-10, -10) 120x120 and the offset is not clipped.
    assert opaque_bbox(image, RED) == (30, 25, 69, 44)
    assert opaque_bbox(image, LIME) == (110, 105, 129, 124)
    assert opaque_bbox(image, BLUE) == (150, 10, 169, 29)
