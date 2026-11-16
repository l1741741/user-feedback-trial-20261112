"""
Pixel-level regression tests for nested viewports and percentages.

These tests cover the resolution of percentage lengths and of some viewport
attributes in nested ``svg`` elements, including ``symbol`` elements used
through ``use``. They render real PNG images and check exact pixel colours at
points chosen analytically from the geometry: no reference image is needed and
the checks never depend on font rendering.

Colours used below:
    white  = (255, 255, 255)
    red    = (255, 0, 0)
    green  = (0, 128, 0)
    blue   = (0, 0, 255)
    black  = (0, 0, 0)
    gray   = (211, 211, 211)

A percentage on an element is resolved against the *active viewport, in the
element's own user coordinate system*: the ``viewBox`` size when there is one
(whatever the viewport size, the mapping done by ``preserveAspectRatio`` and
the viewBox origin), the viewport size otherwise. Leaving a nested ``svg``
restores the parent viewport, so following siblings keep using the outer
reference. A ``symbol`` referenced by ``use`` behaves like a nested ``svg``
with the same viewport.

"""

import io

from PIL import Image

from . import svg2png

WHITE = (255, 255, 255)
RED = (255, 0, 0)
GREEN = (0, 128, 0)
BLUE = (0, 0, 255)
BLACK = (0, 0, 0)
GRAY = (211, 211, 211)


def render(source, width, height):
    """Render ``source`` and return ``(size, pixel accessor)``.

    Transparent pixels are composited on white, so these tests do not depend
    on the background of the surface.

    """
    png = svg2png(source.encode() if isinstance(source, str) else source)
    image = Image.open(io.BytesIO(png)).convert('RGBA')
    background = Image.new('RGBA', image.size, (255, 255, 255, 255))
    image = Image.alpha_composite(background, image).convert('RGB')
    assert image.size == (width, height)
    return image.size, image.load()


def assert_pixels(accessor, expected):
    """Check exact colours at given ``(x, y)`` points."""
    for (x, y), color in expected.items():
        assert accessor[x, y] == color, (
            f'pixel ({x}, {y}) = {accessor[x, y]}, expected {color}')


def document(inner, width=200, height=120):
    """Wrap ``inner`` in a white document of the given size."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'width="{width}" height="{height}">'
        f'<rect width="100%" height="100%" fill="white"/>{inner}</svg>')


def test_percentage_uses_viewbox_size_with_letterboxing():
    """A child percentage inside a letterboxed nested viewport is relative to
    the viewBox, not to the visible viewport."""
    # Viewport 100x50 at (10, 10), viewBox 0 0 10 10, meet: scale 5, content
    # centred horizontally, i.e. 25px of letterbox on each side. 50% of the
    # viewBox is 5 user units, so the red square covers device x 35..59 and
    # y 10..34. The circle (cx/cy 50%, r 10%) is centred at device (59.5,
    # 34.5) with a 5px radius.
    source = document(
        '<svg x="10" y="10" width="100" height="50" viewBox="0 0 10 10">'
        '<rect width="50%" height="50%" fill="red"/>'
        '<circle cx="50%" cy="50%" r="10%" fill="blue"/></svg>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (35, 10): RED,    # top-left corner of the square
        (40, 15): RED,
        (59, 29): RED,    # inside the square, above the circle
        (34, 20): WHITE,  # left letterbox (the viewport starts at x=10)
        (60, 20): WHITE,  # right of the square, still inside the viewport
        (59, 34): BLUE,   # circle centre = centre of the viewport
    })


def test_percentage_reference_is_independent_of_mapping_mode():
    """Letterboxing, cropping and non-uniform scaling do not change the length
    of a user unit, so they do not change the percentage reference. A ``50%``
    rectangle coincides with an explicit 5-unit rectangle in every mode."""
    square = (
        '<svg x="10" y="10" width="100" height="50" viewBox="0 0 10 10" '
        'preserveAspectRatio="{}">'
        '<rect width="50%" height="50%" fill="red"/>'
        '<rect width="5" height="5" fill="blue"/></svg>')
    # meet: scale 5, the 5x5 user square is 25px wide at device x 35..59.
    # slice and none: scale 10 in x, the same square is 50px wide at x 10..59.
    for mode, bounds in (
            ('xMidYMid meet', (35, 10, 59, 34)),
            ('xMidYMid slice', (10, 10, 59, 34)),
            ('none', (10, 10, 59, 34))):
        _, pixels = render(document(square.format(mode)), 200, 120)
        # The blue rectangle is drawn on top of the red one: the red one is
        # entirely hidden, which proves both resolve 50% to 5 user units.
        assert_pixels(pixels, {
            (bounds[0] + 1, bounds[1] + 1): BLUE,
            (bounds[2] - 1, bounds[3] - 1): BLUE,
            (bounds[0] - 1, 20): WHITE,
            (bounds[2] + 1, 20): WHITE,
        })


def test_percentage_with_nonzero_viewbox_origin():
    """Percentages are relative to the viewBox size even when its origin is
    not (0, 0); the origin only translates the content."""
    # meet scale 5, content 50x50 centred vertically in the 100x50 viewport:
    # user (20, 30) maps to device (35, 10). 50% of the viewBox is 5 user
    # units, so the rect ends at device (59, 34).
    source = document(
        '<svg x="10" y="10" width="100" height="50" viewBox="20 30 10 10">'
        '<rect x="20" y="30" width="50%" height="50%" fill="red"/></svg>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (35, 10): RED, (59, 29): RED,
        (34, 20): WHITE,
        (60, 20): WHITE,
        (35, 35): WHITE,  # below the rect, in the vertical letterbox
    })


def test_sibling_after_nested_svg_uses_outer_viewport():
    """Leaving a nested svg restores the parent viewport for percentages."""
    # The nested svg has a 10x10 viewBox in a 100x50 viewport, so its content
    # is 50x50 centred horizontally at x 25..74. The sibling afterwards is
    # sized with 25% of the outer 200x120 viewport, i.e. 50x30.
    source = document(
        '<svg x="0" y="0" width="100" height="50" viewBox="0 0 10 10">'
        '<rect width="10" height="10" fill="green"/></svg>'
        '<rect x="120" y="10" width="25%" height="25%" fill="red"/>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (25, 0): GREEN, (74, 49): GREEN,
        (24, 20): WHITE, (75, 20): WHITE,
        (120, 10): RED, (169, 39): RED,
        (119, 20): WHITE, (170, 20): WHITE, (120, 40): WHITE,
    })


def test_missing_size_does_not_reuse_previous_viewport():
    """A nested svg without width/height uses the parent viewport; it must not
    borrow the size of a previously rendered nested svg."""
    # First viewport: 100x60 at (0, 0), viewBox 10x10, meet scale 6, centred
    # horizontally at x 20..79. Second viewport has no size: 200x60 at
    # (0, 60) (100% of the parent), viewBox 10x10, meet scale 6, centred
    # horizontally at x 40..99. 50% of the viewBox is 5 user units = 30px.
    source = document(
        '<svg x="0" y="0" width="100" height="60" viewBox="0 0 10 10">'
        '<rect width="10" height="10" fill="green"/></svg>'
        '<svg x="0" y="60" viewBox="0 0 10 10">'
        '<rect width="50%" height="50%" fill="red"/></svg>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (20, 0): GREEN, (79, 59): GREEN,
        (40, 60): RED, (99, 89): RED,
        (39, 70): WHITE,  # left letterbox of the second viewport
        (100, 70): WHITE,  # right of it
        (80, 59): WHITE,  # above the second viewport
    })


def test_empty_viewport_renders_nothing():
    """A viewBox with a zero size defines an empty viewport."""
    source = document(
        '<svg x="20" y="10" width="100" height="50" viewBox="0 0 0 10">'
        '<rect width="100%" height="100%" fill="red"/></svg>'
        '<rect x="150" y="90" width="20" height="20" fill="green"/>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (30, 20): WHITE, (50, 30): WHITE, (119, 59): WHITE,
        (155, 95): GREEN,
    })


def test_malformed_viewbox_is_ignored():
    """A viewBox with the wrong number of values or a negative size is
    ignored, as browsers do: the element keeps the parent viewport."""
    # Without a usable viewBox, user units are pixels: the red rect (50%) is
    # 100x60 at (10, 10), clipped to the 100x50 nested viewport.
    for viewbox in ('1 2 3', '0 0 -10 10', '0 0 10 -10', '', 'a b c d'):
        source = document(
            f'<svg x="10" y="10" width="100" height="50" viewBox="{viewbox}">'
            f'<rect width="50%" height="50%" fill="red"/></svg>')
        _, pixels = render(source, 200, 120)
        assert_pixels(pixels, {
            (10, 10): RED, (59, 34): RED,
            (60, 20): WHITE, (10, 35): WHITE,
        })


def test_empty_width_attribute_is_ignored():
    """An empty width/height is invalid: it does not disable the element."""
    source = document(
        '<svg x="10" y="10" width="" height="" viewBox="0 0 10 10">'
        '<rect width="50%" height="50%" fill="red"/></svg>')
    # The nested svg then spans the parent viewport: meet scale 12, the 5x5
    # user rect is 60x60 device pixels at (50, 10).
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (50, 10): RED, (109, 69): RED,
        (49, 40): WHITE, (110, 40): WHITE,
    })


def test_symbol_and_nested_svg_are_equivalent():
    """A symbol referenced by use gives the same geometry as the same content
    directly nested in an svg with the same viewport."""
    inner = (
        '<rect width="50%" height="50%" fill="red"/>'
        '<circle cx="50%" cy="50%" r="10%" fill="blue"/>')
    source = document(
        f'<defs><symbol id="s" viewBox="0 0 10 10">{inner}</symbol></defs>'
        f'<use href="#s" x="10" y="10" width="100" height="50"/>'
        f'<svg x="10" y="70" width="100" height="50" viewBox="0 0 10 10">'
        f'{inner}</svg>', height=140)
    _, pixels = render(source, 200, 140)
    # The second instance is the first one translated by 60px.
    for x, y in ((35, 10), (40, 15), (34, 20), (60, 20), (55, 30)):
        assert pixels[x, y] == pixels[x, y + 60], (x, y)
    assert_pixels(pixels, {
        (35, 10): RED, (59, 29): RED,
        (35, 70): RED, (59, 89): RED,
        (34, 20): WHITE, (60, 20): WHITE,
        (34, 80): WHITE, (60, 80): WHITE,
    })


def test_use_single_dimension_is_applied():
    """Each width/height of a use overrides the referenced viewport
    independently; the missing one comes from the referenced element."""
    # Symbol viewBox 0 0 10 20.
    # Width only: viewport 40x120 (height defaults to 100% of the parent),
    # meet scale 4, content 40x80 centred vertically at y 20..99.
    # Height only: viewport 200x60 at (60, 0), meet scale 3, content 30x60
    # centred horizontally at x 145..174.
    source = document(
        '<defs><symbol id="s" viewBox="0 0 10 20">'
        '<rect width="10" height="20" fill="red"/></symbol></defs>'
        '<use href="#s" x="0" y="0" width="40"/>'
        '<use href="#s" x="60" y="0" height="60"/>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (0, 20): RED, (39, 99): RED,
        (0, 19): WHITE, (39, 100): WHITE,
        (145, 0): RED, (174, 59): RED,
        (144, 30): WHITE, (175, 30): WHITE,
    })


def test_symbol_own_size_and_partial_override():
    """A symbol's own width/height is used when the use specifies none, and
    each dimension can be overridden alone."""
    # Symbol: 20x40, viewBox 10x10. Its child is 50% of the viewBox, i.e. 5x5
    # user units.
    # First use (no size): 20x40 at (10, 10), meet scale 2, content 20x20
    # centred vertically at y 20..39; the child is 10x10 device pixels at
    # x 10..19, y 20..29.
    # Second use (width only): 100x40 (height kept from the symbol), meet
    # scale 4, content 40x40 centred horizontally at x 40..79; the child is
    # 20x20 device pixels at x 40..59, y 60..79.
    source = document(
        '<defs><symbol id="s" width="20" height="40" viewBox="0 0 10 10">'
        '<rect width="50%" height="50%" fill="red"/></symbol></defs>'
        '<use href="#s" x="10" y="10"/>'
        '<use href="#s" x="10" y="60" width="100"/>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (10, 20): RED, (19, 29): RED,
        (10, 19): WHITE, (20, 30): WHITE,
        (40, 60): RED, (59, 79): RED,
        (39, 70): WHITE, (60, 70): WHITE, (80, 70): WHITE,
    })


def test_preserve_aspect_ratio_of_symbol_and_use():
    """The mapping used by ``use`` is the one of the referenced element; a
    ``preserveAspectRatio`` on the ``use`` itself has no effect (as in
    browsers), and the default mapping letterboxes."""
    # On the symbol: a "none" mapping stretches the 10x10 viewBox to the
    # 100x50 viewport, so 50% of 10 user units is 50x25 device pixels and the
    # rect fills device x 10..59, y 10..34.
    source = document(
        '<defs><symbol id="s" viewBox="0 0 10 10" '
        'preserveAspectRatio="none">'
        '<rect width="50%" height="50%" fill="red"/></symbol></defs>'
        '<use href="#s" x="10" y="10" width="100" height="50"/>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (10, 10): RED, (59, 34): RED,
        (60, 20): WHITE, (10, 35): WHITE,
    })
    # Without a value on the symbol, "xMidYMid meet" letterboxes the 10x10
    # viewBox in the 100x50 viewport: the content is a 50x50 square at
    # x 35..84. A preserveAspectRatio on the use does not change that.
    for extra in ('', ' preserveAspectRatio="none"'):
        source = document(
            '<defs><symbol id="s" viewBox="0 0 10 10">'
            '<rect width="50%" height="50%" fill="red"/></symbol></defs>'
            f'<use href="#s" x="10" y="10" width="100" height="50"{extra}/>')
        _, pixels = render(source, 200, 120)
        assert_pixels(pixels, {
            (35, 10): RED, (59, 34): RED,
            (34, 20): WHITE, (60, 20): WHITE, (10, 20): WHITE,
        })


def test_preserve_aspect_ratio_is_not_inherited():
    """preserveAspectRatio on a group must not leak into a nested svg, which
    uses its own value (``xMidYMid meet`` by default)."""
    # meet scale 2 for both nested svgs: the 10x10 viewBox content is 20x20,
    # letterboxed in the 100x20 and 60x20 viewports (at x 40..59 and
    # x 140..159). With "none" inherited, it would fill the whole viewport.
    source = document(
        '<g preserveAspectRatio="none">'
        '<svg x="0" y="0" width="100" height="20" viewBox="0 0 10 10">'
        '<rect width="10" height="10" fill="red"/></svg></g>'
        '<svg x="120" y="0" width="60" height="20" viewBox="0 0 10 10">'
        '<rect width="10" height="10" fill="blue"/></svg>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (40, 0): RED, (59, 19): RED,
        (0, 10): WHITE, (39, 10): WHITE, (99, 19): WHITE,
        (140, 0): BLUE, (159, 19): BLUE,
        (120, 10): WHITE, (179, 19): WHITE,
    })


def test_use_of_svg_keeps_its_own_size():
    """A referenced svg keeps its own width/height, and a single use dimension
    overrides only that dimension."""
    # First use: 20x40 at (10, 10), meet scale 2, the child (5x5 user units) is
    # drawn at x 10..19, y 20..29.
    # Second use (width only): 100x40 at (40, 10), meet scale 4, content 40x40
    # centred horizontally at x 70..109, child at x 70..89, y 10..29.
    source = document(
        '<defs><svg id="s" width="20" height="40" viewBox="0 0 10 10">'
        '<rect width="50%" height="50%" fill="red"/></svg></defs>'
        '<use href="#s" x="10" y="10"/>'
        '<use href="#s" x="40" y="10" width="100"/>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (10, 20): RED, (19, 29): RED,
        (20, 30): WHITE,
        (70, 10): RED, (89, 29): RED,
        (69, 20): WHITE, (90, 20): WHITE,
    })


def test_percentages_of_nested_viewport_are_relative_to_it():
    """A nested svg uses percentages of the parent viewport for its own size,
    and its children use its own viewBox."""
    # Outer viewport: 100x60 at (10, 10) (50% of 200x120), viewBox 10x10, meet
    # scale 6, content 60x60 centred horizontally at x 30..89. Inner viewport:
    # 25% of the outer viewBox = 2.5 user units at (2.5, 2.5), 50% = 5 user
    # units -> device x 45..74, y 25..54.
    source = document(
        '<svg x="10" y="10" width="50%" height="50%" viewBox="0 0 10 10">'
        '<svg x="25%" y="25%" width="50%" height="50%" viewBox="0 0 4 4">'
        '<rect width="100%" height="100%" fill="red"/></svg></svg>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (45, 25): RED, (74, 54): RED,
        (44, 40): WHITE, (75, 40): WHITE,
        (40, 15): WHITE,  # inside the outer viewport, above the inner one
    })


def test_nested_svg_without_size_uses_parent_viewport():
    """Without width/height, a nested svg spans the parent viewport (100%),
    whether or not it has a viewBox. Its size is never taken from the viewBox
    size."""
    # Without a viewBox: user units are pixels, the red rect (50%) is 100x60
    # at (10, 10).
    source = document(
        '<svg x="10" y="10"><rect width="50%" height="50%" '
        'fill="red"/></svg>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (10, 10): RED, (109, 69): RED,
        (110, 40): WHITE, (10, 70): WHITE,
    })
    # With a viewBox 10x10: 50% is 5 user units, meet scale 12, so the rect is
    # 60x60 at (50, 10) inside the 200x120 viewport at (10, 10).
    source = document(
        '<svg x="10" y="10" viewBox="0 0 10 10">'
        '<rect width="50%" height="50%" fill="red"/></svg>')
    _, pixels = render(source, 200, 120)
    assert_pixels(pixels, {
        (50, 10): RED, (109, 69): RED,
        (49, 40): WHITE, (110, 40): WHITE, (50, 9): WHITE,
    })
