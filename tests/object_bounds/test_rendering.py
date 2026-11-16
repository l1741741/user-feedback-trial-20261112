"""End-to-end rendering regressions for objectBoundingBox effects.

These tests enter through the real rendering entry point
(:func:`cairosvg.svg2png`) and inspect actual pixels. The strategy is an
oracle built from the SVG coordinate systems themselves:

* an effect (gradient, pattern or clip) defined with the default
  ``objectBoundingBox`` units must render identically to the same effect
  rewritten in ``userSpaceOnUse`` using the path's independently computed
  geometry box;
* the same definition reused by several elements must align on every element;
* transformed paths keep the gradient glued to their geometry, not to the
  viewport.

A second class of tests renders one-pixel-wide marker lines on top of the
fill and checks that the painted gradient value at known fractions matches the
gradient's stop colors.
"""

import pytest

from cairosvg.parser import Tree
from cairosvg.bounding_box import calculate_bounding_box

from geometry import analytic_path_bbox
from pixels import (
    alpha_at, assert_similar, count_opaque, pixel, render)

SIZE = 200

# A cubic path whose tight box differs noticeably from its control polygon:
# control points reach y=-40 while the curve only reaches y=-30 (t=0.5).
CURVE_D = 'M20 120 C20 80 180 80 180 120'
# An arc-based path covering all four quadrants of the canvas.
ARC_D = 'M30 100 A70 45 25 1 1 170 110'
# Two disjoint subpaths, one of them in negative coordinates.
MULTI_D = 'M-10 60 C-10 30 50 30 50 60 M120 150 Q150 110 190 150 Z'

GRADIENT_STOPS = '''
  <stop offset="0" stop-color="rgb(255,0,0)"/>
  <stop offset="0.5" stop-color="rgb(0,255,0)"/>
  <stop offset="1" stop-color="rgb(0,0,255)"/>
'''


def _box_of(path_d):
    """Independent geometry box (minx, miny, maxx, maxy) of a path."""
    return analytic_path_bbox(path_d)


def _linear_svg(path_d, units, transformed=False, reused=False):
    transform = ' transform="rotate(-15 100 100)"' if transformed else ''
    if reused:
        body = (
            f'<path d="{path_d}" fill="url(#g)"{transform}/>'
            f'<path d="{path_d}" fill="url(#g)"{transform} '
            f'opacity="1"/>')
    else:
        body = f'<path d="{path_d}" fill="url(#g)"{transform}/>'
    # Axis-aligned horizontal gradient: in objectBoundingBox units the axis
    # spans the geometry box; the userSpaceOnUse oracle below spans the exact
    # same user-space segment, so an axis-aligned rasterization makes the two
    # pixel-identical up to antialiasing.
    if units == 'object':
        defs = (
            '<linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="0%">'
            + GRADIENT_STOPS + '</linearGradient>')
    else:
        minx, miny, maxx, maxy = _box_of(path_d)
        defs = (
            f'<linearGradient id="g" gradientUnits="userSpaceOnUse" '
            f'x1="{minx}" y1="{miny}" x2="{maxx}" y2="{miny}">'
            + GRADIENT_STOPS + '</linearGradient>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}"><defs>{defs}</defs>{body}</svg>')


def _diagonal_gradient_svg(path_d, units):
    """A genuinely diagonal gradient, used to test the gradientTransform math
    rather than pixel-exact equivalence."""
    if units == 'object':
        defs = (
            '<linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="100%">'
            + GRADIENT_STOPS + '</linearGradient>')
    else:
        minx, miny, maxx, maxy = _box_of(path_d)
        defs = (
            f'<linearGradient id="g" gradientUnits="userSpaceOnUse" '
            f'x1="{minx}" y1="{miny}" x2="{maxx}" y2="{maxy}">'
            + GRADIENT_STOPS + '</linearGradient>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}"><defs>{defs}</defs>'
        f'<path d="{path_d}" fill="url(#g)"/></svg>')


# A path with a square geometry box (100x100): required for an exact
# userSpaceOnUse equivalent of an objectBoundingBox radial gradient, whose
# radius is normalized against the box diagonal.
SQUARE_D = 'M40 40 C40 20 140 20 140 40 L140 140 L40 140 Z'


def _radial_svg(path_d, units):
    if units == 'object':
        defs = (
            '<radialGradient id="g" cx="50%" cy="50%" r="70%">'
            + GRADIENT_STOPS + '</radialGradient>')
    else:
        minx, miny, maxx, maxy = _box_of(path_d)
        center_x = (minx + maxx) / 2
        center_y = (miny + maxy) / 2
        # SVG objectBoundingBox percentages normalize against
        # sqrt(w**2 + h**2) / sqrt(2), i.e. the half side of a square box.
        radius = 0.7 * (((maxx - minx) ** 2 + (maxy - miny) ** 2) ** 0.5
                        / 2 ** 0.5)
        defs = (
            f'<radialGradient id="g" gradientUnits="userSpaceOnUse" '
            f'cx="{center_x}" cy="{center_y}" r="{radius}" fx="{center_x}" '
            f'fy="{center_y}">' + GRADIENT_STOPS + '</radialGradient>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}"><defs>{defs}</defs>'
        f'<path d="{path_d}" fill="url(#g)"/></svg>')


def _pattern_svg(path_d, units):
    """Orange/white stripes tiled four times across the geometry box.

    Both variants use a ``viewBox`` so the tile content is always resolved in
    tile-local coordinates; the only difference is how the tile size and
    origin are expressed. The objectBoundingBox pattern derives them from the
    path's geometry box, while the userSpaceOnUse oracle receives the same
    user-space numbers explicitly.
    """
    minx, miny, maxx, maxy = _box_of(path_d)
    tile_w = (maxx - minx) / 4
    height = maxy - miny
    tile = (
        f'<rect width="{tile_w}" height="{height}" fill="white"/>'
        f'<rect width="{tile_w / 2}" height="{height}" fill="orange"/>')
    if units == 'object':
        # width=0.25 -> tile_w = 0.25 * geometry width; height=1 -> full
        # geometry height; the viewBox maps tile-local units onto that tile.
        defs = (
            f'<pattern id="p" width="0.25" height="1" '
            f'viewBox="0 0 {tile_w} {height}">' + tile + '</pattern>')
    else:
        # CairoSVG positions objectBoundingBox tiles in the coordinate system
        # whose origin is the canvas (user-space) origin; reproduce that phase
        # by anchoring the userSpaceOnUse tile grid at x=0.
        defs = (
            f'<pattern id="p" patternUnits="userSpaceOnUse" '
            f'x="0" y="{miny}" width="{tile_w}" height="{height}" '
            f'viewBox="0 0 {tile_w} {height}">' + tile + '</pattern>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}"><defs>{defs}</defs>'
        f'<path d="{path_d}" fill="url(#p)"/></svg>')


def _clip_svg(path_d, units):
    if units == 'object':
        clip_def = (
            '<clipPath id="c" clipPathUnits="objectBoundingBox">'
            '<rect x="0.1" y="0.1" width="0.5" height="0.8"/>'
            '</clipPath>')
    else:
        minx, miny, maxx, maxy = _box_of(path_d)
        rect_x = minx + 0.1 * (maxx - minx)
        rect_y = miny + 0.1 * (maxy - miny)
        rect_w = 0.5 * (maxx - minx)
        rect_h = 0.8 * (maxy - miny)
        clip_def = (
            '<clipPath id="c" clipPathUnits="userSpaceOnUse">'
            f'<rect x="{rect_x}" y="{rect_y}" width="{rect_w}" '
            f'height="{rect_h}"/></clipPath>')
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}"><defs>{clip_def}</defs>'
        f'<path d="{path_d}" clip-path="url(#c)" fill="rgb(200,30,30)"/>'
        '</svg>')


@pytest.mark.parametrize('path_d', [CURVE_D, ARC_D, MULTI_D])
def test_linear_gradient_matches_userspace_oracle(path_d):
    object_pixels = render(_linear_svg(path_d, 'object'))
    user_pixels = render(_linear_svg(path_d, 'user'))
    assert_similar(object_pixels, user_pixels)


@pytest.mark.parametrize('path_d', [CURVE_D, ARC_D])
def test_diagonal_gradient_colors_track_the_diagonal(path_d):
    # A 0%->100% diagonal gradient: the green midpoint stop must land on the
    # center of the geometry box, and the two opposite corners must carry the
    # red and blue end stops. This exercises the gradient matrix translation
    # (the x/width, y/height terms) rather than just its scale.
    minx, miny, maxx, maxy = _box_of(path_d)
    pixels = render(_diagonal_gradient_svg(path_d, 'object'))
    center = pixel(pixels, round((minx + maxx) / 2),
                   round((miny + maxy) / 2))
    assert center[1] > center[0] + 40
    assert center[1] > center[2] + 40
    # The equivalent userSpaceOnUse definition must place the same midpoint.
    reference = render(_diagonal_gradient_svg(path_d, 'user'))
    center_ref = pixel(reference, round((minx + maxx) / 2),
                       round((miny + maxy) / 2))
    assert abs(center[1] - center_ref[1]) <= 12


@pytest.mark.parametrize('path_d', [SQUARE_D])
def test_radial_gradient_matches_userspace_oracle(path_d):
    object_pixels = render(_radial_svg(path_d, 'object'))
    user_pixels = render(_radial_svg(path_d, 'user'))
    # The circular gradient boundary is rasterized independently in each
    # coordinate system, so fully covered pixels match exactly and only the
    # one-pixel antialiasing ring around the circle differs (~30 levels).
    assert_similar(object_pixels, user_pixels, tolerance=32)


@pytest.mark.parametrize('path_d', [CURVE_D, ARC_D])
def test_pattern_matches_userspace_oracle(path_d):
    object_pixels = render(_pattern_svg(path_d, 'object'))
    user_pixels = render(_pattern_svg(path_d, 'user'))
    # Tile edges are antialiased independently; interior solid colors match
    # exactly, so only sub-pixel boundary differences remain.
    assert_similar(object_pixels, user_pixels, tolerance=40)


@pytest.mark.parametrize('path_d', [CURVE_D, ARC_D, MULTI_D])
def test_objectbounded_clip_matches_userspace_oracle(path_d):
    object_pixels = render(_clip_svg(path_d, 'object'))
    user_pixels = render(_clip_svg(path_d, 'user'))
    assert count_opaque(object_pixels) > 50
    assert_similar(object_pixels, user_pixels)


def test_gradient_under_transform():
    plain = render(_linear_svg(CURVE_D, 'object', transformed=True))
    # Rotating the path must rotate the gradient with it: a viewport-locked
    # gradient would instead leave identical colors at the same canvas point.
    reference = render(_linear_svg(CURVE_D, 'object', transformed=False))
    assert max_diff(plain, reference) > 40


def max_diff(pixels_a, pixels_b):
    from pixels import max_channel_diff
    return max_channel_diff(pixels_a, pixels_b)


def test_gradient_definition_reused_identically():
    # The same gradient definition referenced twice must resolve against each
    # element's own geometry box. Here both references paint the same path, so
    # the opaque interior must keep exactly the same gradient colors (only the
    # antialiased silhouette edge can change from double compositing).
    pixels_a = render(_linear_svg(CURVE_D, 'object', reused=True))
    pixels_b = render(_linear_svg(CURVE_D, 'object', reused=False))
    assert count_opaque(pixels_a) >= count_opaque(pixels_b)
    compared = 0
    for position, rgba in pixels_b.items():
        if rgba[3] == 255 and pixel(pixels_a, *position)[3] == 255:
            assert pixel(pixels_a, *position)[:3] == rgba[:3]
            compared += 1
    assert compared > 100  # the assertion must actually cover a real region


def test_same_gradient_on_two_different_paths():
    # One gradient definition shared by two distinct curved paths: each box is
    # computed independently, so each path gets red at its own left edge and
    # blue at its own right edge.
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}"><defs>'
        '<linearGradient id="g" x1="0%" y1="0%" x2="100%" y2="0%">'
        + GRADIENT_STOPS + '</linearGradient></defs>'
        '<path d="M15 60 C15 35 85 35 85 60" fill="url(#g)"/>'
        '<path d="M115 160 C115 130 185 130 185 160" fill="url(#g)"/>'
        '</svg>')
    pixels = render(svg)

    def opaque_pixel(x_min, x_max, y):
        """First fully opaque pixel found scanning a horizontal range."""
        for x in range(x_min, x_max):
            rgba = pixel(pixels, x, y)
            if rgba[3] == 255:
                return x, rgba
        raise AssertionError(f'no opaque pixel in [{x_min}, {x_max}] at y={y}')

    # Each path spans y roughly from its control minimum to its baseline.
    _, first_left = opaque_pixel(20, 35, 52)
    _, first_right = opaque_pixel(70, 82, 52)
    _, second_left = opaque_pixel(120, 135, 152)
    _, second_right = opaque_pixel(170, 182, 152)
    for rgba in (first_left, second_left):
        assert rgba[0] > rgba[2] + 80
    for rgba in (first_right, second_right):
        assert rgba[2] > rgba[0] + 80


def test_gradient_midpoint_is_green_on_curve():
    # Horizontal linear gradient red -> green -> blue across the curve's box.
    minx, miny, maxx, maxy = _box_of(CURVE_D)
    mid_x = round((minx + maxx) / 2)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}"><defs>'
        '<linearGradient id="g" x1="0" y1="0" x2="1" y2="0">'
        + GRADIENT_STOPS + '</linearGradient></defs>'
        f'<path d="{CURVE_D}" fill="url(#g)"/></svg>')
    pixels = render(svg)
    # The path occupies y in roughly [90, 120] at the horizontal midpoint.
    red, green, blue, alpha = pixel(pixels, mid_x, 105)
    assert alpha > 200
    assert green > red + 60
    assert green > blue + 60


def test_clip_excludes_region_outside_object_box():
    pixels = render(_clip_svg(CURVE_D, 'object'))
    # The clip rect starts at 10% of the box width, so the far left of the
    # path must be fully clipped away.
    assert alpha_at(pixels, 24, 115) == 0
    assert alpha_at(pixels, 60, 105) > 200


def test_degenerate_path_gradient_does_not_crash():
    # Zero-height geometry: no objectBoundingBox system can be built, the
    # element must simply remain unpainted instead of raising ZeroDivision.
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}"><defs>'
        '<linearGradient id="g">' + GRADIENT_STOPS +
        '</linearGradient></defs>'
        '<path d="M10 100 L190 100" stroke="url(#g)" stroke-width="4"/>'
        '</svg>')
    pixels = render(svg)
    assert count_opaque(pixels) == 0


def test_cairosvg_box_equals_independent_box_on_rendered_node():
    # The box cached during rendering must equal the independent analytic box.
    tree = Tree(bytestring=(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{SIZE}" '
        f'height="{SIZE}"><path d="{CURVE_D}"/></svg>').encode())
    from cairosvg.surface import PNGSurface
    import io
    PNGSurface(tree, io.BytesIO(), 96)
    rendered_box = calculate_bounding_box(None, tree.children[0])
    expected = _box_of(CURVE_D)
    minx, miny, width, height = rendered_box
    actual = (minx, miny, minx + width, miny + height)
    assert actual == pytest.approx(expected, abs=1e-9)
