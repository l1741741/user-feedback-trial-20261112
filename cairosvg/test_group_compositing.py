"""
Group, child and mask compositing tests.

``opacity`` on a container applies to the container's *rendered result*: the
subtree is flattened in isolation and then composited once with a uniform
alpha.  It is not the same as giving each child the same ``opacity`` — for
overlapping children the group result is lighter in the overlap, because the
children see each other before the alpha is applied.

These tests read exact interior pixels of the actual PNG (never file size or
antialiased edges), and cover nesting, clipping, masks, ``use`` and repeated
export of the same parsed tree.

"""

import io

import cairocffi as cairo
import pytest

from . import svg2png
from .parser import Tree
from .surface import PNGSurface

BACKGROUND = '#ffffff'
HEADER = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
    '<rect width="100" height="100" fill="{background}"/>')


def render(body, background=BACKGROUND):
    """Return the rendered RGBA image as ``(get_pixel, width, height)``."""
    source = HEADER.format(background=background) + body + '</svg>'
    png = svg2png(bytestring=source.encode())
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    surface.flush()
    data, stride = bytes(surface.get_data()), surface.get_stride()

    def pixel(x, y):
        offset = y * stride + x * 4
        blue, green, red, alpha = data[offset:offset + 4]
        return red, green, blue, alpha

    return pixel


def assert_pixels(pixel, expected):
    """Check ``{point: rgba}`` against the rendered image."""
    for point, rgba in expected.items():
        assert pixel(point[0], point[1]) == rgba, (
            f'pixel {point}: {pixel(point[0], point[1])} != {rgba}')


# Two overlapping squares, red painted before blue.
SHAPES = (
    '<rect x="10" y="10" width="60" height="60" fill="#f00"/>'
    '<rect x="30" y="30" width="60" height="60" fill="#00f"/>')
# Same squares, each painted with its own opacity.
SHAPES_OPACITY = (
    '<rect x="10" y="10" width="60" height="60" fill="#f00" opacity="0.5"/>'
    '<rect x="30" y="30" width="60" height="60" fill="#00f" opacity="0.5"/>')

# Shared sampling points: red only, overlap, blue only, background.
RED = (15, 15)
OVERLAP = (40, 40)
BLUE = (80, 80)
OUTSIDE = (95, 95)


def test_group_opacity_is_not_per_child_opacity():
    """Group opacity flattens first; per-child opacity does not.

    With per-child opacity, blue at 50% over red at 50% over white gives
    (127, 63, 191) in the overlap: the backdrop shows through both layers.
    Flattening red+blue first and then applying 50% gives (127, 127, 255)
    instead, identical to the blue-only area, because the opaque blue covers
    the red *inside* the group before any alpha is applied.
    """
    group = render(f'<g opacity="0.5">{SHAPES}</g>')
    children = render(f'<g>{SHAPES_OPACITY}</g>')

    assert_pixels(group, {
        RED: (255, 127, 127, 255),
        OVERLAP: (127, 127, 255, 255),
        BLUE: (127, 127, 255, 255),
        OUTSIDE: (255, 255, 255, 255),
    })
    assert_pixels(children, {
        RED: (255, 127, 127, 255),
        OVERLAP: (127, 63, 191, 255),
        BLUE: (127, 127, 255, 255),
    })
    assert group(OVERLAP[0], OVERLAP[1]) != children(OVERLAP[0], OVERLAP[1])


def test_nested_group_opacities_multiply():
    """0.5 inside 0.5 is one alpha of 0.25, not two independent passes."""
    pixel = render(
        f'<g opacity="0.5"><g opacity="0.5">{SHAPES}</g></g>')
    # Over the white background a 0.25 red is (255, 191, 191).
    assert_pixels(pixel, {
        RED: (255, 191, 191, 255),
        # Overlap: blue (0.25) over red over white.
        OVERLAP: (191, 191, 255, 255),
        BLUE: (191, 191, 255, 255),
        OUTSIDE: (255, 255, 255, 255),
    })


def test_group_opacity_combines_with_child_opacity():
    """A child's own opacity is applied before the group's."""
    pixel = render(
        '<g opacity="0.5">'
        '<rect x="10" y="10" width="60" height="60" fill="#f00" '
        'opacity="0.5"/></g>')
    # 0.5 child alpha flattened, then 0.5 group alpha: 0.25 over white.
    assert_pixels(pixel, {RED: (255, 191, 191, 255)})


def test_group_opacity_with_clip_path():
    """Clipping restricts the flattened group, it does not change its alpha."""
    pixel = render(
        '<defs><clipPath id="c">'
        '<rect x="0" y="0" width="50" height="50"/></clipPath></defs>'
        f'<g opacity="0.5" clip-path="url(#c)">{SHAPES}</g>')
    assert_pixels(pixel, {
        RED: (255, 127, 127, 255),
        # Inside the clip: same composition as the unclipped group.
        OVERLAP: (127, 127, 255, 255),
        # Outside the clip nothing is painted, including the blue square.
        BLUE: (255, 255, 255, 255),
    })


def test_group_opacity_with_mask_applies_alpha_once():
    """Group opacity and mask both apply exactly once.

    A fully opaque mask is a no-op, so group opacity plus a white mask must
    match group opacity alone; a 50% mask halves the result.  If the opacity
    were also folded into the mask it would be squared and leave a 25%
    composite here.
    """
    defs = (
        '<defs>'
        '<mask id="full"><rect width="100" height="100" fill="#fff"/></mask>'
        '<mask id="half"><rect width="100" height="100" fill="#fff" '
        'fill-opacity="0.5"/></mask>'
        '</defs>')

    plain = render(f'<g opacity="0.5">{SHAPES}</g>')
    full = render(
        f'{defs}<g opacity="0.5" mask="url(#full)">{SHAPES}</g>')
    half = render(
        f'{defs}<g opacity="0.5" mask="url(#half)">{SHAPES}</g>')

    for point in (RED, OVERLAP, BLUE, OUTSIDE):
        assert full(*point) == plain(*point)

    # 50% mask then 50% opacity: 0.25 of the flattened result over white.
    assert_pixels(half, {
        RED: (255, 191, 191, 255),
        OVERLAP: (191, 191, 255, 255),
        BLUE: (191, 191, 255, 255),
    })


def test_leaf_opacity_with_mask_applies_alpha_once():
    """A leaf's opacity with a mask must not be multiplied twice."""
    defs = (
        '<defs>'
        '<mask id="full"><rect width="100" height="100" fill="#fff"/></mask>'
        '<mask id="half"><rect width="100" height="100" fill="#fff" '
        'fill-opacity="0.5"/></mask>'
        '</defs>')
    leaf = '<rect x="10" y="10" width="60" height="60" fill="#f00"/>'

    plain = render(f'<rect x="10" y="10" width="60" height="60" '
                   'fill="#f00" opacity="0.5"/>')
    assert_pixels(plain, {RED: (255, 127, 127, 255)})

    full = render(
        f'{defs}<rect x="10" y="10" width="60" height="60" fill="#f00" '
        'opacity="0.5" mask="url(#full)"/>')
    assert full(*RED) == plain(*RED)

    half = render(
        f'{defs}<rect x="10" y="10" width="60" height="60" fill="#f00" '
        'opacity="0.5" mask="url(#half)"/>')
    # 0.5 opacity and 0.5 mask -> 0.25 over white, not 0.125.
    assert_pixels(half, {RED: (255, 191, 191, 255)})


def test_use_inherits_opacity_across_the_referenced_subtree():
    """``use`` with opacity composites the referenced subtree as a group."""
    pixel = render(
        f'<defs><g id="u">{SHAPES}</g></defs>'
        '<use href="#u" opacity="0.5"/>')
    assert_pixels(pixel, {
        RED: (255, 127, 127, 255),
        OVERLAP: (127, 127, 255, 255),
        BLUE: (127, 127, 255, 255),
    })


def test_use_positions_the_reference_without_mutating_it():
    """``use`` keeps its own x/y and leaves the referenced node untouched."""
    pixel = render(
        f'<defs><g id="u">'
        '<rect x="0" y="0" width="20" height="20" fill="#f00"/></g></defs>'
        '<use href="#u"/>'
        '<use href="#u" x="60" y="60"/>')
    assert_pixels(pixel, {
        (10, 10): (255, 0, 0, 255),
        (70, 70): (255, 0, 0, 255),
        (90, 10): (255, 255, 255, 255),
    })


def test_leaf_opacity_with_filter_applies_alpha_once():
    """A filter isolates the element; opacity must not also enter the paint."""
    filter_ = (
        '<defs><filter id="f">'
        '<feGaussianBlur stdDeviation="0"/></filter></defs>')
    plain = render(
        '<rect x="10" y="10" width="60" height="60" fill="#00f" '
        'opacity="0.5"/>')
    filtered = render(
        f'{filter_}<rect x="10" y="10" width="60" height="60" fill="#00f" '
        'opacity="0.5" filter="url(#f)"/>')
    assert_pixels(plain, {OVERLAP: (127, 127, 255, 255)})
    # Filtering with a zero blur is a no-op, so the alpha is the same.
    assert filtered(*OVERLAP) == plain(*OVERLAP)


def test_raster_image_opacity_with_mask_applies_alpha_once():
    """A raster image's opacity is not applied both here and by the mask."""
    import base64

    from PIL import Image

    buffer = io.BytesIO()
    Image.new('RGB', (10, 10), (255, 0, 0)).save(buffer, 'PNG')
    data = base64.b64encode(buffer.getvalue()).decode()
    image = (
        f'<image x="10" y="10" width="50" height="50" '
        f'href="data:image/png;base64,{data}"')
    mask = (
        '<defs><mask id="m">'
        '<rect width="100" height="100" fill="#fff"/></mask></defs>')

    plain = render(image + ' opacity="0.5"/>')
    isolated = render(mask + image + ' opacity="0.5" mask="url(#m)"/>')
    grouped = render(f'<g opacity="0.5">{image}/></g>')
    nested = render(f'<g opacity="0.5">{image} opacity="0.5"/></g>')

    assert_pixels(plain, {(30, 30): (255, 127, 127, 255)})
    # A fully opaque mask is a no-op, so the image alpha is applied once.
    assert isolated(30, 30) == plain(30, 30)
    # Group opacity alone also gives 0.5; group 0.5 around image 0.5 gives
    # 0.25, i.e. the image opacity is applied inside the isolated group.
    assert_pixels(grouped, {(30, 30): (255, 127, 127, 255)})
    assert_pixels(nested, {(30, 30): (255, 191, 191, 255)})


def test_transparent_and_empty_groups_leave_siblings_untouched():
    """An empty or fully transparent group must not leak state to siblings."""
    green = '<rect x="50" y="50" width="40" height="40" fill="#0a0"/>'
    expected = render(green)

    empty = render(f'<g opacity="0.5"/>{green}')
    transparent = render(
        '<g opacity="0">'
        '<rect x="10" y="10" width="60" height="60" fill="#f00"/></g>'
        f'{green}')

    for point in (RED, OVERLAP, (80, 80), OUTSIDE, (55, 55)):
        assert empty(*point) == expected(*point)
        assert transparent(*point) == expected(*point)


def test_repeated_export_of_one_tree_is_identical():
    """Rendering the same parsed tree twice must produce the same pixels.

    Masks, patterns and ``use`` references are stored in the shared tree and
    used to be rewritten in place, so the second export drifted.
    """
    source = (
        b'<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        b'<defs>'
        b'<mask id="m">'
        b'<rect width="100" height="100" fill="#fff" fill-opacity="0.5"/>'
        b'</mask>'
        b'<pattern id="p" width="10" height="10" '
        b'patternUnits="userSpaceOnUse">'
        b'<rect width="5" height="5" fill="#00f"/></pattern>'
        b'<g id="u">'
        b'<rect x="10" y="10" width="60" height="60" fill="#f00"/>'
        b'<rect x="30" y="30" width="60" height="60" fill="#00f"/>'
        b'</g>'
        b'</defs>'
        b'<rect width="100" height="100" fill="#fff"/>'
        b'<g opacity="0.5" mask="url(#m)">'
        b'<use href="#u"/><use href="#u" x="5" y="5"/>'
        b'</g>'
        b'<rect x="70" y="70" width="20" height="20" fill="url(#p)"/>'
        b'</svg>')

    tree = Tree(bytestring=source)
    outputs = []
    for _ in range(3):
        output = io.BytesIO()
        surface = PNGSurface(tree, output, 96)
        surface.finish()
        outputs.append(output.getvalue())
    assert outputs[0] == outputs[1] == outputs[2]


def test_mask_reused_at_different_sizes():
    """A mask resolved against one element's size must not leak to the next."""
    pixel = render(
        '<defs><mask id="m">'
        '<rect width="100" height="100" fill="#fff"/></mask></defs>'
        '<rect x="5" y="5" width="20" height="20" fill="#f00" '
        'mask="url(#m)"/>'
        '<rect x="40" y="40" width="50" height="50" fill="#00f" '
        'mask="url(#m)"/>')
    assert_pixels(pixel, {
        (10, 10): (255, 0, 0, 255),
        (60, 60): (0, 0, 255, 255),
        (85, 85): (0, 0, 255, 255),
        (35, 35): (255, 255, 255, 255),
    })


@pytest.mark.parametrize('body', [
    # No opacity anywhere: ground truth for "compatible output".
    '<rect x="5" y="10" width="13" height="15" fill="lime" stroke="black"/>',
    # A single element with opacity, still folded into its paint.
    '<rect x="5" y="10" width="13" height="15" fill="lime" stroke="black" '
    'opacity="0.5"/>',
])
def test_output_has_expected_shape(body):
    """Opacity-free and single-element documents keep rendering normally."""
    pixel = render(body)
    assert pixel(0, 0) == (255, 255, 255, 255)
    assert pixel(11, 17) in ((0, 255, 0, 255), (127, 255, 127, 255))
    assert pixel(95, 95) == (255, 255, 255, 255)
