"""Pixel tests for stroke joins, caps and miter limits.

These tests render small SVGs at a high scale and inspect the resulting pixels.
They are fully offline: no font, no external resource and no network access is
used. Probe points are chosen inside or outside a shape, away from the
antialiased outline, so they are stable across Cairo versions.

Reference values were checked against an independent SVG renderer (Chromium)
and against Cairo's own stroker, so they encode SVG semantics rather than
CairoSVG's current behaviour.

"""

import io
import math

import pytest
from PIL import Image

from . import svg2png

# Device scale used for every rendering: 1 user unit is 4 pixels, which makes
# the joins and caps large enough to inspect one pixel inside their outline.
SCALE = 4
SIZE = 100


def render(body, width=SIZE, height=SIZE, scale=SCALE):
    """Render an SVG document and return the RGBA image."""
    source = f'''<svg xmlns="http://www.w3.org/2000/svg"
        xmlns:xlink="http://www.w3.org/1999/xlink"
        width="{width}" height="{height}" viewBox="0 0 {width} {height}">
      {body}
    </svg>'''
    png = svg2png(
        bytestring=source.encode(), output_width=width * scale,
        output_height=height * scale)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def ink_bounding_box(image, threshold=8):
    """Return the bounding box of the painted pixels, in user units."""
    mask = image.getchannel('A').point(
        lambda value: 255 if value > threshold else 0)
    box = mask.getbbox()
    if box is None:
        return None
    return tuple(round(position / SCALE, 2) for position in box)


def ink_top(image, x_user, threshold=8):
    """Return the topmost painted row at ``x_user``, in user units."""
    column = round(x_user * SCALE)
    alpha = image.getchannel('A')
    for y in range(image.height):
        if alpha.getpixel((column, y)) > threshold:
            return round(y / SCALE, 2)
    return None


def ink_count(image, threshold=8):
    """Return the number of painted pixels."""
    return sum(image.getchannel('A').histogram()[threshold + 1:])


def pixel(image, x_user, y_user):
    """Return the RGBA value at a user-space position."""
    return image.getpixel((round(x_user * SCALE), round(y_user * SCALE)))


def alpha(image, x_user, y_user):
    """Return the alpha value at a user-space position."""
    return pixel(image, x_user, y_user)[3]


# A sharp V whose apex is at (50, 20): its miter ratio is 5/3 ≈ 1.67, far
# below the default limit of 4, so a miter limit of 10 or 4 paints the sharp
# tip while 1 paints a bevel. The tip reaches y ≈ 15, a bevel stops at y ≈ 17.
SHARP_V = 'M20 60 L50 20 L80 60'


# ---------------------------------------------------------------------------
# stroke-linejoin
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('join', ['miter', 'round', 'bevel'])
def test_join_shapes_differ_on_sharp_angle(join):
    """Each line join paints a distinguishable outline on a sharp V."""
    image = render(
        f'<path d="{SHARP_V}" fill="none" stroke="black" stroke-width="6"'
        f' stroke-linejoin="{join}" stroke-miterlimit="10"/>')
    top = ink_top(image, 50)
    assert top is not None
    if join == 'miter':
        # The miter tip extends well above the apex.
        assert top < 17
    else:
        # Round and bevel stop just above the vertex.
        assert 16 < top < 20


def test_explicit_miter_overrides_inherited_round():
    """A path asking for miter inside a round group gets a miter.

    The child attribute used to be ignored when it asked for the value that
    was not handled specially (miter), so the inherited round join was kept.
    """
    inherited = render(
        '<g fill="none" stroke="black" stroke-width="6"'
        f' stroke-linejoin="round"><path d="{SHARP_V}"/></g>')
    explicit = render(
        '<g fill="none" stroke="black" stroke-width="6"'
        f' stroke-linejoin="round"><path d="{SHARP_V}"'
        ' stroke-linejoin="miter" stroke-miterlimit="10"/></g>')
    assert ink_top(explicit, 50) < 16
    assert ink_top(inherited, 50) > 16


def test_explicit_bevel_overrides_inherited_miter():
    """A path asking for bevel inside a miter group gets a bevel."""
    inherited = render(
        '<g fill="none" stroke="black" stroke-width="6"'
        f' stroke-linejoin="miter" stroke-miterlimit="10">'
        f'<path d="{SHARP_V}"/></g>')
    explicit = render(
        '<g fill="none" stroke="black" stroke-width="6"'
        f' stroke-linejoin="miter" stroke-miterlimit="10">'
        f'<path d="{SHARP_V}" stroke-linejoin="bevel"/></g>')
    assert ink_top(explicit, 50) > ink_top(inherited, 50) + 2


@pytest.mark.parametrize('value', ['round', 'ROUND', 'Round', ' round '])
def test_join_keyword_is_case_insensitive(value):
    """Paint keywords are matched case-insensitively, as SVG requires."""
    image = render(
        f'<path d="{SHARP_V}" fill="none" stroke="black" stroke-width="6"'
        f' stroke-linejoin="{value}"/>')
    assert 16 < ink_top(image, 50) < 20


def test_join_changes_are_visible_on_curves_and_closed_subpaths():
    """Line, quadratic, cubic and closing segments follow the chosen join.

    The apex of the straight part is sharp, and the cubic comes back to the
    closing line at a sharp angle too. The same path is stroked with miter and
    with bevel; both sharp corners must show the difference.
    """
    path = 'M20 78 L50 18 L80 78 Q50 60 20 78 Z'
    miter = render(
        f'<path d="{path}" fill="none" stroke="black" stroke-width="5"'
        ' stroke-linejoin="miter" stroke-miterlimit="10"/>')
    bevel = render(
        f'<path d="{path}" fill="none" stroke="black" stroke-width="5"'
        ' stroke-linejoin="bevel"/>')
    # The miter tip at the apex extends above and below the bevel outline.
    assert ink_top(miter, 50) < ink_top(bevel, 50)
    assert ink_bounding_box(miter)[3] > ink_bounding_box(bevel)[3]


# ---------------------------------------------------------------------------
# stroke-linecap
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('cap,expected_start', [
    ('butt', 30), ('round', 25), ('square', 25)])
def test_linecap_extents(cap, expected_start):
    """Butt caps stop at the endpoint, round and square extend by half."""
    image = render(
        f'<path d="M30 50 L70 50" fill="none" stroke="black"'
        f' stroke-width="10" stroke-linecap="{cap}"/>')
    left, _, right, _ = ink_bounding_box(image)
    assert abs(left - expected_start) <= 1
    assert abs(right - (100 - expected_start)) <= 1


def test_explicit_butt_overrides_inherited_round_cap():
    """A path asking for butt inside a round group loses the round cap."""
    inherited = render(
        '<g stroke="black" fill="none" stroke-width="10"'
        ' stroke-linecap="round"><path d="M30 50 L70 50"/></g>')
    explicit = render(
        '<g stroke="black" fill="none" stroke-width="10"'
        ' stroke-linecap="round"><path d="M30 50 L70 50"'
        ' stroke-linecap="butt"/></g>')
    assert ink_bounding_box(inherited)[0] < ink_bounding_box(explicit)[0] - 2


def test_explicit_square_overrides_inherited_butt_cap():
    """A path asking for square inside a butt group gets a square cap."""
    inherited = render(
        '<g stroke="black" fill="none" stroke-width="10"'
        ' stroke-linecap="butt"><path d="M30 50 L70 50"/></g>')
    explicit = render(
        '<g stroke="black" fill="none" stroke-width="10"'
        ' stroke-linecap="butt"><path d="M30 50 L70 50"'
        ' stroke-linecap="square"/></g>')
    assert ink_bounding_box(inherited)[0] > ink_bounding_box(explicit)[0] + 2


@pytest.mark.parametrize('value', ['square', 'SQUARE', 'Square', ' square '])
def test_linecap_keyword_is_case_insensitive(value):
    """Cap keywords are matched case-insensitively."""
    image = render(
        f'<path d="M30 50 L70 50" fill="none" stroke="black"'
        f' stroke-width="10" stroke-linecap="{value}"/>')
    assert abs(ink_bounding_box(image)[0] - 25) <= 1


# ---------------------------------------------------------------------------
# Invalid values must follow the existing compatibility rules
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('attribute,value', [
    ('stroke-linejoin', 'foo'),
    ('stroke-linejoin', ''),
    ('stroke-linecap', 'foo'),
    ('stroke-linecap', ''),
    ('stroke-miterlimit', 'abc'),
    ('stroke-miterlimit', '4px'),
    ('stroke-miterlimit', '400%'),
    ('stroke-miterlimit', 'nan'),
    ('stroke-miterlimit', 'inf'),
    ('stroke-miterlimit', '-1'),
])
def test_invalid_stroke_values_do_not_raise(attribute, value):
    """Invalid paint values are ignored instead of aborting the rendering."""
    attributes = f'{attribute}="{value}"'
    if attribute != 'stroke-linejoin':
        attributes += ' stroke-linejoin="miter"'
    render(
        f'<path d="{SHARP_V}" fill="none" stroke="black" stroke-width="6"'
        f' {attributes}/>')


@pytest.mark.parametrize('value', ['abc', '4px', '400%', 'nan', 'inf', '-1'])
def test_invalid_miterlimit_keeps_inherited_value(value):
    """An invalid miter limit leaves the inherited value in effect."""
    body = (
        '<g fill="none" stroke="black" stroke-width="6"'
        ' stroke-linejoin="miter" stroke-miterlimit="10">'
        f'<path d="{SHARP_V}" stroke-miterlimit="{value}"/>'
        '</g>')
    image = render(body)
    # The inherited limit of 10 still allows the sharp miter.
    assert ink_top(image, 50) < 17


def test_zero_miterlimit_is_forwarded_to_cairo():
    """A numeric 0 keeps its historic Cairo behaviour (no miter).

    It is not treated as an invalid value, so documents relying on the
    previous behaviour keep rendering the same.
    """
    image = render(
        f'<path d="{SHARP_V}" fill="none" stroke="black" stroke-width="6"'
        ' stroke-linejoin="miter" stroke-miterlimit="0"/>')
    assert ink_top(image, 50) > 16


def test_infinite_miterlimit_keeps_the_stroke_visible():
    """An infinite miter limit must not make Cairo drop the whole stroke.

    Cairo accepts ``inf`` but then paints nothing, which made the element
    disappear instead of just falling back to bevel joins.
    """
    image = render(
        f'<path d="{SHARP_V}" fill="none" stroke="black" stroke-width="6"'
        ' stroke-linejoin="miter" stroke-miterlimit="inf"/>')
    assert ink_bounding_box(image) is not None
    assert ink_count(image) > 0


@pytest.mark.parametrize('dash', [
    '10 -5', '10 5 -3', '-5 5', '0 0', '0', '10 0 0', '10 nan', 'inf 5'])
def test_invalid_dasharray_does_not_poison_the_context(dash):
    """A useless dash list is ignored instead of raising CAIRO_INVALID_DASH.

    The same document also draws a plain rectangle afterwards: a poisoned
    Cairo context would make that second drawing raise.
    """
    image = render(
        '<path d="M10 20 L90 20" fill="none" stroke="black" stroke-width="4"'
        f' stroke-dasharray="{dash}"/>'
        '<rect x="40" y="60" width="20" height="20" fill="red"/>')
    # The rectangle is drawn whatever the dash list does on the path.
    assert pixel(image, 50, 70) == (255, 0, 0, 255)


# ---------------------------------------------------------------------------
# Zero-length subpaths
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('element', [
    'path', 'line', 'polyline', 'cubic', 'quadratic', 'move-close'])
def test_zero_length_subpath_with_square_cap(element):
    """A single-point subpath is painted as a square with ``square`` caps."""
    bodies = {
        'path': '<path d="M20 20 L20 20" stroke-width="16" fill="none"'
                ' stroke="black" stroke-linecap="square"/>',
        'line': '<line x1="20" y1="20" x2="20" y2="20" stroke-width="16"'
                ' stroke="black" stroke-linecap="square"/>',
        'polyline': '<polyline points="20,20 20,20" stroke-width="16"'
                    ' fill="none" stroke="black" stroke-linecap="square"/>',
        'cubic': '<path d="M20 20 c0 0 0 0 0 0" stroke-width="16"'
                 ' fill="none" stroke="black" stroke-linecap="square"/>',
        'quadratic': '<path d="M20 20 q0 0 0 0" stroke-width="16"'
                     ' fill="none" stroke="black" stroke-linecap="square"/>',
        'move-close': '<path d="M20 20 Z" stroke-width="16" fill="none"'
                      ' stroke="black" stroke-linecap="square"/>',
    }
    image = render(bodies[element])
    # The square spans 8 units on each side of the point.
    assert ink_bounding_box(image) == (12, 12, 28, 28)


@pytest.mark.parametrize('element', [
    'path', 'line', 'polyline', 'cubic', 'quadratic', 'move-close'])
def test_zero_length_subpath_with_round_cap(element):
    """A single-point subpath is painted as a dot with ``round`` caps."""
    bodies = {
        'path': '<path d="M20 20 L20 20" stroke-width="16" fill="none"'
                ' stroke="black" stroke-linecap="round"/>',
        'line': '<line x1="20" y1="20" x2="20" y2="20" stroke-width="16"'
                ' stroke="black" stroke-linecap="round"/>',
        'polyline': '<polyline points="20,20 20,20" stroke-width="16"'
                    ' fill="none" stroke="black" stroke-linecap="round"/>',
        'cubic': '<path d="M20 20 c0 0 0 0 0 0" stroke-width="16"'
                 ' fill="none" stroke="black" stroke-linecap="round"/>',
        'quadratic': '<path d="M20 20 q0 0 0 0" stroke-width="16"'
                     ' fill="none" stroke="black" stroke-linecap="round"/>',
        'move-close': '<path d="M20 20 Z" stroke-width="16" fill="none"'
                      ' stroke="black" stroke-linecap="round"/>',
    }
    image = render(bodies[element])
    assert ink_bounding_box(image) == (12, 12, 28, 28)
    # A dot is round: its corners are not painted, unlike a square cap.
    assert alpha(image, 13, 13) == 0


@pytest.mark.parametrize('element', [
    'path', 'line', 'polyline', 'cubic', 'quadratic', 'move-close'])
def test_zero_length_subpath_with_butt_cap_paints_nothing(element):
    """SVG defines no cap for a single point with ``butt`` caps."""
    bodies = {
        'path': '<path d="M20 20 L20 20" stroke-width="16" fill="none"'
                ' stroke="black" stroke-linecap="butt"/>',
        'line': '<line x1="20" y1="20" x2="20" y2="20" stroke-width="16"'
                ' stroke="black" stroke-linecap="butt"/>',
        'polyline': '<polyline points="20,20 20,20" stroke-width="16"'
                    ' fill="none" stroke="black" stroke-linecap="butt"/>',
        'cubic': '<path d="M20 20 c0 0 0 0 0 0" stroke-width="16"'
                 ' fill="none" stroke="black" stroke-linecap="butt"/>',
        'quadratic': '<path d="M20 20 q0 0 0 0" stroke-width="16"'
                     ' fill="none" stroke="black" stroke-linecap="butt"/>',
        'move-close': '<path d="M20 20 Z" stroke-width="16" fill="none"'
                      ' stroke="black" stroke-linecap="butt"/>',
    }
    assert ink_bounding_box(render(bodies[element])) is None


def test_move_only_paints_no_cap():
    """A lone ``M`` does not start a subpath, so no cap is drawn."""
    for cap in ('butt', 'round', 'square'):
        image = render(
            f'<path d="M20 20" stroke-width="16" fill="none"'
            f' stroke="black" stroke-linecap="{cap}"/>')
        assert ink_bounding_box(image) is None


def test_zero_length_subpath_beside_regular_geometry():
    """The caps are added next to the real geometry, which is kept intact."""
    body = (
        '<path d="M70 50 L90 50 M20 20 L20 20" fill="none" stroke="black"'
        ' stroke-width="8" stroke-linecap="square"/>')
    image = render(body)
    # 70..90 with a square cap reaches x=94; the dot spans 16..24.
    assert ink_bounding_box(image) == (16, 16, 94, 54)
    # The ordinary segment is still stroked.
    assert alpha(image, 80, 50) == 255


def test_zero_length_dot_uses_dash_phase_and_paint_source():
    """Dots follow the dash pattern and the paint source of the element."""
    dashed = render(
        '<path d="M20 20 L20 20" fill="none" stroke="black"'
        ' stroke-width="8" stroke-linecap="square" stroke-dasharray="10 10"'
        ' stroke-dashoffset="0"/>')
    assert ink_bounding_box(dashed) is not None
    outside_dash = render(
        '<path d="M20 20 L20 20" fill="none" stroke="black"'
        ' stroke-width="8" stroke-linecap="square" stroke-dasharray="10 10"'
        ' stroke-dashoffset="10"/>')
    assert ink_bounding_box(outside_dash) is None

    gradient = render(
        '<defs><linearGradient id="g" gradientUnits="userSpaceOnUse"'
        ' x1="0" y1="0" x2="100" y2="0">'
        '<stop offset="0" stop-color="red"/>'
        '<stop offset="1" stop-color="blue"/></linearGradient></defs>'
        '<path d="M20 50 L20 50" fill="none" stroke="url(#g)"'
        ' stroke-width="20" stroke-linecap="round"/>')
    assert pixel(gradient, 20, 50)[3] == 255


def test_zero_length_cap_is_transparent_to_fill_geometry():
    """Filling ignores single-point subpaths (they have no area)."""
    image = render(
        '<path d="M30 30 L70 30 L70 70 L30 70 Z M10 10 L10 10"'
        ' fill="red" stroke="none"/>')
    assert pixel(image, 50, 50) == (255, 0, 0, 255)
    assert alpha(image, 10, 10) == 0


# ---------------------------------------------------------------------------
# Transformed sharp angles and short segments
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('transform', [
    '', 'translate(5 5)', 'rotate(20 50 50)', 'scale(1.2 1.2)'])
def test_sharp_miter_under_transforms(transform):
    """The miter tip is still painted when the element is transformed."""
    image = render(
        f'<g transform="{transform}"><path d="{SHARP_V}" fill="none"'
        ' stroke="black" stroke-width="6" stroke-linejoin="miter"'
        ' stroke-miterlimit="10"/></g>')
    assert ink_bounding_box(image) is not None


def test_short_middle_segment_keeps_miter():
    """A very short segment between two long ones joins with a miter."""
    image = render(
        '<path d="M15 70 L60 60 L60.2 60.1 L85 20" fill="none"'
        ' stroke="black" stroke-width="8" stroke-linejoin="miter"'
        ' stroke-miterlimit="10"/>')
    assert ink_bounding_box(image) is not None
    assert ink_count(image) > 2000


# ---------------------------------------------------------------------------
# Object bounding boxes used by paint servers and clips
# ---------------------------------------------------------------------------

def test_object_bounding_box_of_path_curves():
    """Curve bounding boxes follow the curve, not its control polygon.

    A quadratic curve whose control point is outside the drawn curve used to
    inflate the box; its true highest point is halfway between the endpoints.
    """
    from .bounding_box import bounding_box_path
    assert bounding_box_path(None, {'d': 'M10 50 Q50 5 90 50'}) == (
        10, 27.5, 80, 22.5)
    assert bounding_box_path(None, {'d': 'M10 50 C10 5 90 5 90 50'}) == (
        10, 16.25, 80, 33.75)


def test_object_bounding_box_of_smooth_curves():
    """``T`` and ``S`` take the reflected control point into account."""
    from .bounding_box import bounding_box_path
    box = bounding_box_path(None, {'d': 'M10 90 Q50 10 90 90 T90 10'})
    # The reflected control point is at (130, 170) and pushes the box right.
    assert box[0] == 10 and box[2] > 90


def test_object_bounding_box_clip_on_path_element():
    """``clipPathUnits="objectBoundingBox"`` works on a path element.

    Paths have no ``x``/``y``/``width``/``height`` attributes, so the clip path
    used to be scaled by zero and raised CAIRO_STATUS_INVALID_MATRIX.
    """
    image = render(
        '<defs><clipPath id="c" clipPathUnits="objectBoundingBox">'
        '<rect width="0.5" height="1"/></clipPath></defs>'
        '<path d="M10 10 L90 10 L90 90 L10 90 Z" fill="red"'
        ' clip-path="url(#c)"/>')
    assert ink_bounding_box(image) == (10, 10, 50, 90)
    assert pixel(image, 30, 50) == (255, 0, 0, 255)
    assert alpha(image, 70, 50) == 0


def test_object_bounding_box_clip_on_circle_element():
    """The same applies to shapes without layout attributes."""
    image = render(
        '<defs><clipPath id="c" clipPathUnits="objectBoundingBox">'
        '<rect width="0.5" height="1"/></clipPath></defs>'
        '<circle cx="50" cy="50" r="40" fill="red" clip-path="url(#c)"/>')
    assert pixel(image, 30, 50) == (255, 0, 0, 255)
    assert alpha(image, 70, 50) == 0


def test_gradient_object_bounding_box_uses_geometry():
    """A stroke-only path has no fill box: the gradient paints nothing.

    This mirrors the SVG definition of the object bounding box (geometry only,
    never the stroke) and matches other renderers.
    """
    image = render(
        '<defs><linearGradient id="g"><stop offset="0" stop-color="red"/>'
        '<stop offset="1" stop-color="blue"/></linearGradient></defs>'
        '<path d="M10 50 L90 50" fill="none" stroke="url(#g)"'
        ' stroke-width="8" stroke-linecap="round"/>')
    assert ink_bounding_box(image) is None


def test_marker_content_without_area_does_not_crash():
    """A marker drawn with a straight line has no bounding box.

    It used to raise ``TypeError`` while computing the marker scale.
    """
    image = render(
        '<defs><marker id="m" markerWidth="10" markerHeight="10"'
        ' refX="5" refY="5" markerUnits="userSpaceOnUse">'
        '<path d="M0 5 L10 5" stroke="red" stroke-width="2"/>'
        '</marker></defs>'
        '<path d="M10 50 L90 50" stroke="black" stroke-width="3"'
        ' marker-end="url(#m)"/>')
    # The path itself is still painted even if the marker cannot be scaled.
    assert ink_bounding_box(image) is not None


# ---------------------------------------------------------------------------
# Reuse must not change the source node
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('body', [
    # ``use`` of a path with markers.
    '<defs><path id="p" d="M10 20 L50 10 L90 20" fill="none"'
    ' stroke="black" stroke-width="4"/></defs>'
    '<use xlink:href="#p"/><use xlink:href="#p" y="40"/>',
    # ``use`` of a group.
    '<defs><g id="g"><rect width="30" height="10" fill="red"/></g></defs>'
    '<use xlink:href="#g" x="10" y="10"/><use xlink:href="#g" x="50" y="50"/>',
    # ``use`` of a symbol with a viewBox.
    '<defs><symbol id="s" viewBox="0 0 10 10">'
    '<rect width="10" height="10" fill="blue"/></symbol></defs>'
    '<use xlink:href="#s" x="10" y="10" width="20" height="20"/>'
    '<use xlink:href="#s" x="40" y="40" width="30" height="30"/>',
    # A mask used by two elements.
    '<defs><mask id="m"><rect width="50" height="100" fill="white"/>'
    '</mask></defs><rect width="100" height="100" fill="red" mask="url(#m)"/>',
    # A pattern using object bounding box units, used twice.
    '<defs><pattern id="p" width="0.2" height="0.2">'
    '<rect width="0.1" height="0.1" fill="red"/></pattern></defs>'
    '<rect x="10" y="10" width="30" height="30" fill="url(#p)"/>'
    '<rect x="50" y="50" width="40" height="40" fill="url(#p)"/>',
])
def test_rendering_the_same_tree_twice_is_stable(body):
    """Rendering the same parsed tree again yields the same image.

    ``use`` and paint servers used to write resolved values on the shared
    nodes, so the second rendering of a tree differed from the first one.
    """
    from . import parser, surface

    tree = parser.Tree(bytestring=(
        '<svg xmlns="http://www.w3.org/2000/svg"'
        ' xmlns:xlink="http://www.w3.org/1999/xlink" width="100"'
        f' height="100">{body}</svg>').encode())

    def convert():
        output = io.BytesIO()
        instance = surface.PNGSurface(tree, output, 96)
        instance.finish()
        return output.getvalue()

    first = convert()
    second = convert()
    third = convert()
    assert first == second == third


def test_use_does_not_delete_attributes_of_the_source_node():
    """``use`` must not remove ``x``, ``y``, ``viewBox`` or ``mask``."""
    from . import parser, surface

    tree = parser.Tree(bytestring=(
        '<svg xmlns="http://www.w3.org/2000/svg"'
        ' xmlns:xlink="http://www.w3.org/1999/xlink" width="100"'
        ' height="100"><defs><symbol id="s" viewBox="0 0 10 10">'
        '<rect width="10" height="10"/></symbol></defs>'
        '<use xlink:href="#s" x="10" y="20" width="30" height="30"/>'
        '</svg>').encode())
    use_nodes = [
        child for child in tree.children if child.tag == 'use']
    assert len(use_nodes) == 1
    use_node = use_nodes[0]
    assert (use_node.get('x'), use_node.get('y')) == ('10', '20')

    surface.PNGSurface(tree, io.BytesIO(), 96).finish()
    assert (use_node.get('x'), use_node.get('y')) == ('10', '20')
    assert use_node.get('width') == '30'
    assert use_node.get('height') == '30'
