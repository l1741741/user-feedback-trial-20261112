"""
Pixel-level tests for SVG ``transform`` and ``transform-origin`` support.

The fixtures are small, font-free SVGs using plain colors. Probe points are
chosen away from antialiased edges so exact RGB comparisons are stable across
Cairo versions. The expected geometry was cross-checked against Chrome's
``getScreenCTM()`` and its rendering of the same markup, which resolves
``transform-box`` and ``transform-origin`` per CSS Transforms Level 1.

``transform-box`` is a CSS property: browsers only honour it through the
``style`` attribute (it is not a presentation attribute), which is also how
the tests exercise it. CairoSVG additionally accepts it as a plain attribute.

Colors:
    background = (245, 245, 245)
    red        = (221, 51, 51)
    blue       = (0, 0, 187)

"""

import io

import pytest

from . import svg2png

BACKGROUND = (245, 245, 245)
RED = (221, 51, 51)
BLUE = (0, 0, 187)


def pixels(svg_source):
    """Render ``svg_source`` and return ``(image, pixel_accessor)``."""
    from PIL import Image
    png = svg2png(svg_source.encode() if isinstance(svg_source, str)
                  else svg_source)
    image = Image.open(io.BytesIO(png)).convert('RGB')
    return image, image.load()


def assert_pixels(accessor, expected):
    """Assert that each ``(x, y): color`` probe matches exactly."""
    for (x, y), color in expected.items():
        assert accessor[x, y] == color, (
            f'pixel ({x}, {y}) = {accessor[x, y]}, expected {color}')


def assert_color_bounds(accessor, size, color, expected, tolerance=2):
    """Assert the bounding box of ``color`` pixels equals ``expected``."""
    width, height = size
    xs, ys = [], []
    for y in range(height):
        for x in range(width):
            if accessor[x, y] == color:
                xs.append(x)
                ys.append(y)
    if expected is None:
        assert not xs, f'unexpected {color} pixels'
        return
    assert xs, f'no {color} pixels found'
    found = min(xs), min(ys), max(xs), max(ys)
    assert all(abs(a - b) <= tolerance for a, b in zip(found, expected)), (
        f'{color} bounds = {found}, expected {expected}')


def page(inner, width=360, height=240, viewbox='0 0 360 240'):
    """Wrap ``inner`` in a plain page the pixel probes can measure."""
    return f'''<svg xmlns="http://www.w3.org/2000/svg"
                width="{width}" height="{height}" viewBox="{viewbox}">
      <rect width="{width}" height="{height}" fill="#f5f5f5"/>
      {inner}
    </svg>'''


def test_default_origin_is_reference_box_top_left():
    """Without ``transform-origin`` the element scales about ``0 0`` of its
    view box, not about the middle of the surface."""
    image, px = pixels(page(
        '<rect x="100" y="150" width="100" height="20" fill="#d33" '
        'transform="scale(0.5)"/>'))
    # scale(0.5) about (0, 0) maps the rect to x 50..100, y 75..85.
    assert_color_bounds(px, image.size, RED, (50, 75, 99, 84))


def test_transform_list_order():
    """A transform list applies left to right, each function nested in the
    previous one: ``translate scale`` differs from ``scale translate``."""
    image, px = pixels(page(
        '<rect x="10" y="10" width="10" height="10" fill="#d33" '
        'transform="translate(100,50) scale(2)"/>'))
    # (10,10) -> scale -> (20,20) -> translate -> (120,70), 20x20.
    assert_color_bounds(px, image.size, RED, (120, 70, 139, 89))

    image, px = pixels(page(
        '<rect x="10" y="10" width="10" height="10" fill="#d33" '
        'transform="scale(2) translate(100,50)"/>'))
    # (10,10) -> translate -> (110,60) -> scale -> (220,120), 20x20.
    assert_color_bounds(px, image.size, RED, (220, 120, 239, 139))


def test_transform_matrix_ordering():
    """``matrix`` composes with the other functions in list order."""
    image, px = pixels(page(
        '<rect x="10" y="10" width="10" height="10" fill="#d33" '
        'transform="matrix(2,0,0,2,0,0) translate(100,50)"/>'))
    # translate first, then matrix: (10,10) -> (110,60) -> (220,120).
    assert_color_bounds(px, image.size, RED, (220, 120, 239, 139))

    image, px = pixels(page(
        '<rect x="10" y="10" width="10" height="10" fill="#d33" '
        'transform="translate(100,50) matrix(2,0,0,2,0,0)"/>'))
    # matrix first, then translate: (10,10) -> (20,20) -> (120,70).
    assert_color_bounds(px, image.size, RED, (120, 70, 139, 89))


def test_nested_group_order():
    """A child transform acts in its parent group's user space: the group
    transform is the outer one."""
    image, px = pixels(page(
        '<g transform="translate(200,0)">'
        '<rect x="10" y="10" width="10" height="10" fill="#d33" '
        'transform="scale(2)"/></g>'))
    # (10,10) -> scale -> (20,20) -> group translate -> (220,20).
    assert_color_bounds(px, image.size, RED, (220, 20, 239, 39))

    image, px = pixels(page(
        '<g transform="scale(2)">'
        '<rect x="10" y="10" width="10" height="10" fill="#d33" '
        'transform="translate(100,0)"/></g>'))
    # (10,10) -> translate -> (110,10) -> group scale -> (220,20), 20x20.
    assert_color_bounds(px, image.size, RED, (220, 20, 239, 39))


def test_skew_and_translate_order():
    """``skewX`` and ``translate`` compose in list order: the leftmost is the
    outer one, so the two orders give different placements."""
    image, px = pixels(page(
        '<rect x="0" y="0" width="10" height="4" fill="#d33" '
        'transform="translate(100,40) skewX(45)"/>'))
    # skewX first (x' = x + y), then translate: (0,0) -> (100,40),
    # (10,4) -> (14,4) -> (114,44).
    assert_color_bounds(px, image.size, RED, (100, 40, 114, 44))

    image, px = pixels(page(
        '<rect x="0" y="0" width="10" height="4" fill="#d33" '
        'transform="skewX(45) translate(100,40)"/>'))
    # translate first, then skewX: (0,0) -> (100,40) -> (140,40),
    # (10,4) -> (110,44) -> (154,44).
    assert_color_bounds(px, image.size, RED, (140, 40, 154, 44))


def test_transform_origin_lengths_and_percentages():
    """Explicit lengths and percentages resolve against the view box."""
    image, px = pixels(page(
        '<rect x="170" y="110" width="20" height="20" fill="#d33" '
        'transform="rotate(90)" transform-origin="180 120"/>'))
    assert_pixels(px, {(180, 120): RED})

    image, px = pixels(page(
        '<rect x="170" y="110" width="20" height="20" fill="#d33" '
        'transform="rotate(90)" transform-origin="50% 50%"/>'))
    # 50% of the 360x240 view box is (180, 120): the same fixed point.
    assert_pixels(px, {(180, 120): RED})


def test_transform_origin_keywords():
    """One- and two-value keyword forms, including keyword order."""
    # ``left top`` is (0, 0): rotate(90) moves the rect off the left edge.
    image, px = pixels(page(
        '<rect x="10" y="10" width="20" height="20" fill="#d33" '
        'transform="rotate(90)" transform-origin="left top"/>'))
    assert_color_bounds(px, image.size, RED, None)

    # ``right bottom`` is (360, 240): the rect maps beyond the bottom-right
    # corner, so nothing is painted inside the page.
    image, px = pixels(page(
        '<rect x="170" y="110" width="20" height="20" fill="#d33" '
        'transform="rotate(90)" transform-origin="right bottom"/>'))
    assert_color_bounds(px, image.size, RED, None)

    # A single ``center`` means ``center center``: (180, 120).
    image, px = pixels(page(
        '<rect x="10" y="10" width="20" height="20" fill="#d33" '
        'transform="rotate(90)" transform-origin="center"/>'))
    # (10,10) rotates to (-10, 10); (30,30) to (-30, 30): off canvas.
    assert_color_bounds(px, image.size, RED, None)


def test_one_value_origin_keyword_axes():
    """A single``left`` fixes x and centres y; a single ``top`` fixes y."""
    image, px = pixels(page(
        '<rect x="10" y="10" width="20" height="20" fill="#d33" '
        'transform="rotate(90)" transform-origin="left"/>'))
    # Origin (0, 120): (10,10) -> (110, 130), (30,30) -> (90, 150).
    assert_color_bounds(px, image.size, RED, (90, 130, 109, 149))

    image, px = pixels(page(
        '<rect x="100" y="100" width="40" height="20" fill="#d33" '
        'transform="rotate(90)" transform-origin="top"/>'))
    # Origin (180, 0): y is fixed, x is centred. (100,100) -> (-100, 100).
    assert_color_bounds(px, image.size, RED, None)


def test_transform_box_fill_box():
    """``fill-box`` resolves percentages against the object bounding box."""
    image, px = pixels(page(
        '<rect x="100" y="80" width="100" height="40" fill="#d33" '
        'transform="rotate(90)" '
        'style="transform-box:fill-box;transform-origin:50% 50%"/>'))
    # Box centre (150, 100); rotate(90) maps the 100x40 box to 40x100 there.
    assert_color_bounds(px, image.size, RED, (130, 50, 169, 149))


def test_transform_box_stroke_box():
    """``stroke-box`` extends the box by half the painted stroke width."""
    body = (
        'x="100" y="80" width="100" height="40" fill="#d33" stroke="#00b" '
        'stroke-width="20" transform="scale(0.5)" '
        'style="transform-box:{box};transform-origin:0 0"')
    image, px = pixels(page('<rect ' + body.format(box='fill-box') + '/>'))
    # fill-box starts at (100, 80); scale(0.5) keeps (100, 80) fixed.
    assert_color_bounds(px, image.size, RED, (105, 85, 144, 94))

    image, px = pixels(page('<rect ' + body.format(box='stroke-box') + '/>'))
    # stroke-box starts at (90, 70): the fixed point moves up-left.
    assert_color_bounds(px, image.size, RED, (100, 80, 139, 89))
    assert_color_bounds(px, image.size, BLUE, (90, 70, 149, 99))


def test_transform_box_view_box_ignores_viewbox_offset():
    """``view-box`` percentages use the view box size and its 0 0 origin,
    even when the viewBox has a non-zero min-x/min-y."""
    image, px = pixels(page(
        '<rect x="90" y="90" width="20" height="20" fill="#d33" '
        'transform="rotate(90)" transform-box="view-box" '
        'transform-origin="50% 50%"/>',
        width=200, height=200, viewbox='100 100 200 200'))
    # 50% 50% of the 200x200 view box is (100, 100) in user space, which is
    # the page origin after the viewBox translation. The rotated square
    # therefore covers the page corner.
    assert_color_bounds(px, image.size, RED, (0, 0, 9, 9))


def test_transform_box_on_group_and_use():
    """The box of a ``g`` covers its children, and the box of a ``use`` is
    the referenced content."""
    image, px = pixels(page(
        '<g transform="rotate(90)" '
        'style="transform-box:fill-box;transform-origin:50% 50%">'
        '<rect x="100" y="80" width="100" height="40" fill="#d33"/>'
        '<rect x="260" y="160" width="40" height="40" fill="#00b"/></g>'))
    # Group box x 100..300, y 80..200, centre (200, 140); the red child
    # rotates to a 40x100 box left of the centre line.
    assert_color_bounds(px, image.size, RED, (220, 40, 259, 139))

    image, px = pixels(page(
        '<defs><rect id="r" x="100" y="80" width="100" height="40" '
        'fill="#d33"/></defs>'
        '<use href="#r" transform="rotate(90)" '
        'style="transform-box:fill-box;transform-origin:50% 50%"/>'))
    assert_color_bounds(px, image.size, RED, (130, 50, 169, 149))


@pytest.mark.parametrize('transform_origin', [
    'bogus 50%', '50% bogus', 'left right', '1px 2px 3px 4px', ''])
def test_invalid_origin_falls_back_to_box_origin(transform_origin):
    """An invalid ``transform-origin`` falls back to ``0 0`` of the reference
    box (its initial value) instead of corrupting the element or the tree."""
    image, px = pixels(page(
        f'<rect x="100" y="80" width="100" height="40" fill="#d33" '
        f'transform="scale(0.5)" transform-origin="{transform_origin}"/>'))
    # scale(0.5) about (0, 0): x 50..100, y 40..60.
    assert_color_bounds(px, image.size, RED, (50, 40, 99, 59))


@pytest.mark.parametrize('transform', [
    'scale(2) bogus(3)', 'rotate(45deg)', 'translate(10%, 5%)',
    'translateX(10)', 'matrix(1,0,0,1,10)', 'rotate(30, 10)',
])
def test_invalid_transform_is_ignored(transform):
    """An invalid transform draws the element untransformed, without raising
    and without affecting the following elements."""
    image, px = pixels(page(
        f'<rect x="100" y="80" width="100" height="40" fill="#d33" '
        f'transform="{transform}"/>'
        f'<rect x="40" y="180" width="20" height="20" fill="#00b"/>'))
    assert_color_bounds(px, image.size, RED, (100, 80, 199, 119))
    assert_color_bounds(px, image.size, BLUE, (40, 180, 59, 199))


def test_degenerate_transform_hides_element():
    """A non-invertible matrix clips the element away; a following sibling
    still paints normally."""
    image, px = pixels(page(
        '<rect x="100" y="80" width="100" height="40" fill="#d33" '
        'transform="scale(0)"/>'
        '<rect x="40" y="180" width="20" height="20" fill="#00b"/>'))
    assert_color_bounds(px, image.size, RED, None)
    assert_color_bounds(px, image.size, BLUE, (40, 180, 59, 199))


def test_reuse_is_idempotent():
    """Rendering the same parsed tree twice produces identical pixels.

    ``use`` and ``image`` used to delete ``x``/``y`` from the referenced node
    and patterns used to mutate their cached definition, so a second render
    moved content or reused a stale transform.
    """
    from PIL import Image

    from .parser import Tree
    from .surface import PNGSurface

    source = '''<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">
      <defs>
        <rect id="r" x="0" y="0" width="40" height="20" fill="#d33"/>
        <pattern id="p" width="10" height="10" patternUnits="userSpaceOnUse">
          <rect width="5" height="5" fill="#00b"/>
        </pattern>
      </defs>
      <use href="#r" x="60" y="40" transform="rotate(20)"
           style="transform-box:fill-box;transform-origin:50% 50%"/>
      <use href="#r" x="120" y="140"/>
      <rect x="0" y="0" width="40" height="40" fill="url(#p)"/>
    </svg>'''
    tree = Tree(bytestring=source.encode())
    renders = []
    for _ in range(2):
        output = io.BytesIO()
        PNGSurface(tree, output, 96).finish()
        image = Image.open(io.BytesIO(output.getvalue())).convert('RGBA')
        renders.append(image.tobytes())
    assert renders[0] == renders[1]


def test_root_svg_transform_origin_defaults_to_centre():
    """A transform on the outermost ``svg`` uses the viewport centre as its
    initial origin and applies outside the viewBox mapping."""
    _, px = pixels(page(
        '<rect x="170" y="110" width="20" height="20" fill="#d33"/>'
    ).replace('<svg ', '<svg transform="rotate(90)" ', 1))
    # rotate(90) about the default origin (180, 120) keeps it fixed.
    assert_pixels(px, {(180, 120): RED})


def test_root_svg_transform_accepts_explicit_origin():
    """An explicit origin on the outermost ``svg`` overrides the default."""
    image, px = pixels(page(
        '<rect x="170" y="110" width="20" height="20" fill="#d33"/>'
    ).replace(
        '<svg ', '<svg transform="scale(0.5)" transform-origin="0 0" ', 1))
    # scale(0.5) about (0, 0): x 85..95, y 55..65.
    assert_color_bounds(px, image.size, RED, (85, 55, 94, 64))


def test_pattern_content_units_object_bounding_box():
    """Patterns drawn in their own sub-surface keep their computed
    ``transform`` (object bounding box units scale the pattern content).

    This guards the ordering between the root ``svg`` transform shortcut and
    the per-element transform: a pattern tree is *not* the outermost surface,
    so its scale must still be applied by the regular element path.
    """
    image, px = pixels(
        '<svg xmlns="http://www.w3.org/2000/svg" width="120" height="120">'
        '<defs><pattern id="p" width="0.5" height="0.5" '
        'patternContentUnits="objectBoundingBox">'
        '<rect width="0.5" height="0.5" fill="#d33"/>'
        '</pattern></defs>'
        '<rect x="10" y="10" width="100" height="100" fill="url(#p)"/>'
        '</svg>')
    # The whole target is tiled: the red content covers the pattern tile and
    # is repeated over the 100x100 rect. Black outside it is the page.
    assert_pixels(px, {
        (11, 11): RED, (60, 60): RED, (109, 109): RED,
        (0, 0): (0, 0, 0), (119, 119): (0, 0, 0),
    })


@pytest.mark.parametrize('transform', [
    'rotate(30, 10)', 'rotate(45deg)', 'translate(10px, 20px)',
])
def test_function_argument_validation(transform):
    """Each transform function takes a fixed number of unitless arguments;
    anything else invalidates the whole attribute."""
    image, px = pixels(page(
        f'<rect x="100" y="80" width="100" height="40" fill="#d33" '
        f'transform="{transform}"/>'))
    assert_color_bounds(px, image.size, RED, (100, 80, 199, 119))


def test_rotate_with_center_and_origin():
    """``rotate(angle, cx, cy)`` rotates about (cx, cy) *before* the
    transform-origin conjugation, and the two compose in the list order."""
    image, px = pixels(page(
        '<rect x="100" y="80" width="100" height="40" fill="#d33" '
        'transform="rotate(90, 150, 100)" transform-origin="0 0"/>'))
    # rotate about (150, 100) maps the 100x40 box to 40x100 around it.
    assert_color_bounds(px, image.size, RED, (130, 50, 169, 149))


def test_three_value_origin_is_2d():
    """The third ``transform-origin`` value is a z offset; it must not change
    the 2D result. A percentage there is invalid."""
    image, px = pixels(page(
        '<rect x="170" y="110" width="20" height="20" fill="#d33" '
        'transform="rotate(90)" transform-origin="180 120 5px"/>'))
    assert_pixels(px, {(180, 120): RED})

    image, px = pixels(page(
        '<rect x="100" y="80" width="100" height="40" fill="#d33" '
        'transform="scale(0.5)" transform-origin="10px 20px 5%"/>'))
    # Invalid: falls back to the box origin, like other invalid values.
    assert_color_bounds(px, image.size, RED, (50, 40, 99, 59))


def test_origin_relative_units_use_element_font_size():
    """``em``/``ex``/``ch`` lengths resolve against the element font size."""
    image, px = pixels(page(
        '<rect x="100" y="80" width="100" height="40" fill="#d33" '
        'font-size="10" transform="scale(0.5)" '
        'transform-origin="20em 20em"/>'))
    # 20em at font-size 10 is 200: origin (200, 200), so scale(0.5) maps
    # (100,80) -> (150,140).
    assert_color_bounds(px, image.size, RED, (150, 140, 199, 159))

    image, px = pixels(page(
        '<rect x="100" y="80" width="100" height="40" fill="#d33" '
        'font-size="10" transform="scale(0.5)" '
        'transform-origin="10em 8em"/>'))
    # 10em/8em = (100, 80): the box's top-left, which scale(0.5) keeps fixed.
    assert_color_bounds(px, image.size, RED, (100, 80, 149, 99))
