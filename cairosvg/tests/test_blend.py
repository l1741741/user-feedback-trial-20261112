"""
Tests for the color and alpha handling of the supported feBlend modes.

The expected values are computed by hand from the W3C compositing and
blending formulas (see https://www.w3.org/TR/compositing-1/), using straight
(non-premultiplied) RGBA colors; a small tolerance covers 8-bit rounding.

"""

import io
import warnings
from pathlib import Path

import cairocffi as cairo
import pytest

from .. import svg2png

SVG_FOLDER = Path(__file__).parent / 'svg'

# 8-bit quantization tolerance for one compositing step.
TOLERANCE = 2


def render(svg_name, background_color=None):
    """Render one test SVG and return the Cairo image surface."""
    png = svg2png(
        url=str(SVG_FOLDER / svg_name),
        background_color=background_color)
    return cairo.ImageSurface.create_from_png(io.BytesIO(png))


def pixel(surface, x, y):
    """Return the straight RGBA value of one pixel as integers in 0..255."""
    buffer = bytes(surface.get_data())
    width = surface.get_width()
    blue, green, red, alpha = buffer[
        4 * (y * width + x):4 * (y * width + x) + 4]
    if alpha == 0:
        return red, green, blue, 0
    return (round(255 * red / alpha), round(255 * green / alpha),
            round(255 * blue / alpha), alpha)


def assert_pixel(surface, x, y, expected):
    """Check one pixel, straight color and coverage separately."""
    red, green, blue, alpha = pixel(surface, x, y)
    expected_red, expected_green, expected_blue, expected_alpha = expected
    assert abs(red - expected_red) <= TOLERANCE
    assert abs(green - expected_green) <= TOLERANCE
    assert abs(blue - expected_blue) <= TOLERANCE
    assert abs(alpha - expected_alpha) <= TOLERANCE


def test_overlap_multiply_matches_hand_calculation():
    """Group multiply over an empty backdrop keeps source-over values."""
    surface = render('feBlend_overlap_multiply.svg')
    # Red only: red at 50% coverage.
    assert_pixel(surface, 10, 20, (255, 0, 0, 128))
    # Blue only.
    assert_pixel(surface, 45, 20, (0, 0, 255, 128))
    # Overlap: a=.75, straight (R=1/3, B=2/3) -> (85, 0, 170), alpha 192.
    assert_pixel(surface, 30, 20, (85, 0, 170, 192))
    # Outside of the shapes: fully transparent, no hidden color.
    assert_pixel(surface, 2, 2, (0, 0, 0, 0))


def test_multiply_over_empty_equals_plain_source_over():
    """Blending against a transparent backdrop must be a no-op."""
    filtered = render('feBlend_overlap_multiply.svg')
    reference = render('feBlend_overlap_reference.svg')
    assert bytes(filtered.get_data()) == bytes(reference.get_data())


def test_leaf_opacity_is_applied_once():
    """filter + opacity on a leaf must not square the coverage."""
    surface = render('feBlend_leaf_opacity_screen.svg')
    # Single 50%-opaque rect: coverage stays 128, straight color stays pure.
    assert_pixel(surface, 10, 20, (255, 0, 0, 128))
    assert_pixel(surface, 45, 20, (0, 0, 255, 128))
    # Overlap keeps the 75% coverage instead of the old squared 25%.
    # Screen over an empty backdrop is a no-op on the source-over result,
    # here straight (R=2/3, B=2/3) -> (170, 0, 170).
    assert_pixel(surface, 30, 20, (170, 0, 170, 192))


def test_transparent_gradient_has_no_dark_fringe():
    """Hidden color of fully transparent pixels never reaches the result."""
    surface = render('feBlend_transparent_gradient.svg')
    for x in (0, 10, 20, 30, 35, 38, 39):
        red, green, blue, alpha = pixel(surface, x, 10)
        # Straight color is always pure red; only coverage fades.
        assert red == 255
        assert green == 0
        assert blue == 0
        if x < 39:
            assert alpha > 0
    assert pixel(surface, 39, 10)[3] < 8


def test_transparent_gradient_on_white_background():
    """On an opaque background the blended fringe follows the gradient."""
    surface = render(
        'feBlend_transparent_gradient.svg', background_color='white')
    # Opaque end: almost pure red (first gradient pixel is alpha 252/255).
    red, green, blue, alpha = pixel(surface, 0, 10)
    assert alpha == 255 and red == 255 and green == blue <= 4
    # Nearly transparent end: only a faint red tint remains over white.
    red, green, blue, alpha = pixel(surface, 39, 10)
    assert alpha == 255
    assert red == 255
    assert green == blue >= 250
    # The middle is red fading toward white with full coverage.
    red, green, blue, alpha = pixel(surface, 20, 10)
    assert alpha == 255
    assert red == 255
    assert 0 < green == blue < 255

def test_all_supported_modes_on_white():
    """Check the five modes against hand values on a white backdrop."""
    surface = render('feBlend_modes_on_white.svg')
    # normal and multiply/darken give pink (255, 128, 128), 8-bit -> 127.
    for center_x in (20, 70, 170):
        assert_pixel(surface, center_x, 15, (255, 128, 128, 255))
    # screen and lighten give white.
    for center_x in (120, 220):
        assert_pixel(surface, center_x, 15, (255, 255, 255, 255))


def test_shared_filter_definition_is_not_mutated():
    """Two references to one filter render identically."""
    surface = render('feBlend_shared_filter.svg')
    assert_pixel(surface, 10, 10, (255, 0, 0, 128))
    assert_pixel(surface, 50, 10, (255, 0, 0, 128))


def test_unsupported_mode_warns_and_keeps_normal_compositing():
    """Unknown modes keep the historical behavior and warn explicitly."""
    svg = b'''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">
      <defs><filter id="f"><feBlend mode="overlay"/></filter></defs>
      <rect x="0" y="0" width="20" height="20" fill="red"
            opacity="0.5" filter="url(#f)"/>
    </svg>'''
    with warnings.catch_warnings(record=True) as records:
        warnings.simplefilter('always')
        png = svg2png(bytestring=svg)
    assert any(
        'overlay' in str(record.message) for record in records)
    surface = cairo.ImageSurface.create_from_png(io.BytesIO(png))
    # Fallback is normal source-over: pure red at 50% coverage.
    assert_pixel(surface, 10, 10, (255, 0, 0, 128))


def test_rendering_order_does_not_change_coverage():
    """Swapping the rectangles keeps coverage and single-color areas.

    The overlap straight color depends on the source-over order, but its
    alpha and the non-overlapping areas must stay identical.
    """
    ordered = render('feBlend_overlap_multiply.svg')
    swapped = cairo.ImageSurface.create_from_png(io.BytesIO(svg2png(
        bytestring=b'''<svg
        xmlns="http://www.w3.org/2000/svg" width="60" height="40"
        shape-rendering="crispEdges">
      <defs><filter id="f"><feBlend mode="multiply"/></filter></defs>
      <g filter="url(#f)">
        <rect x="20" y="5" width="30" height="30" fill="blue" opacity="0.5"/>
        <rect x="5" y="5" width="30" height="30" fill="red" opacity="0.5"/>
      </g>
    </svg>''')))
    # Coverage (alpha channel) is order-independent everywhere.
    def alphas(surface):
        return bytes(surface.get_data())[3::4]
    assert alphas(ordered) == alphas(swapped)
    # The swapped overlap has red on top of blue: straight (170, 0, 85).
    assert_pixel(swapped, 30, 20, (170, 0, 85, 192))
    # Single-color areas do not depend on the order.
    assert_pixel(swapped, 10, 20, (255, 0, 0, 128))
    assert_pixel(swapped, 45, 20, (0, 0, 255, 128))


def test_default_rendering_without_filter_is_unchanged():
    """Plain shapes must render exactly as before the filter changes."""
    surface = render('feBlend_overlap_reference.svg')
    assert_pixel(surface, 10, 20, (255, 0, 0, 128))
    assert_pixel(surface, 30, 20, (85, 0, 170, 192))
    assert_pixel(surface, 45, 20, (0, 0, 255, 128))


@pytest.mark.parametrize('background', (None, 'white', 'black'))
def test_background_variants(background):
    """Blend output on transparent, white and black canvases stays valid."""
    surface = render(
        'feBlend_leaf_opacity_screen.svg', background_color=background)
    red, green, blue, alpha = pixel(surface, 10, 20)
    if background is None:
        # Empty backdrop: the 50% coverage is preserved, color stays pure.
        assert (red, green, blue, alpha) == (255, 0, 0, 128)
    else:
        # An opaque background always gives full coverage.
        assert alpha == 255
        if background == 'white':
            # Screen of red over white is white.
            assert (red, green, blue) == (255, 255, 255)
        else:
            # Screen of red over black: co = .5*red -> straight (128, 0, 0).
            assert (red, green, blue) == (128, 0, 0)
