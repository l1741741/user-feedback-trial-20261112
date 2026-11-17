"""
Tests for CSS color parsing and for the alpha composition of colors.

These tests cover the color notations accepted by :mod:`cairosvg.colors`
(hex colors with alpha, comma and space separated ``rgb()`` / ``rgba()``
with number, percentage and slash alpha syntax) and check that the color's
own alpha channel combines exactly once with fill-opacity, stroke-opacity,
stop-opacity and group opacity.

Rendering tests compare the actual pixels produced by CairoSVG, so no
reference bitmap is required.

"""

import io

import pytest
from hypothesis import given, strategies as st
from PIL import Image

from .colors import INVALID_COLOR, color
from . import svg2png


# --- helpers ---------------------------------------------------------------

def render_pixels(svg, size=(10, 10)):
    """Render ``svg`` and return the image as an RGBA Pillow image."""
    if isinstance(svg, str):
        svg = svg.encode()
    png = svg2png(
        bytestring=svg,
        output_width=size[0], output_height=size[1])
    return Image.open(io.BytesIO(png)).convert('RGBA')


def center_pixel(svg, size=(10, 10)):
    return render_pixels(svg, size).getpixel((size[0] // 2, size[1] // 2))


def rect_svg(fill, opacity_attributes='', size=(10, 10)):
    return f'''<svg xmlns="http://www.w3.org/2000/svg"
        width="{size[0]}" height="{size[1]}">
      <rect width="{size[0]}" height="{size[1]}"
            fill="{fill}" {opacity_attributes}/>
    </svg>'''


def approx_pixel(actual, expected, tolerance=1):
    assert actual[:3] == pytest.approx(expected[:3], abs=tolerance)
    assert actual[3] == pytest.approx(expected[3], abs=tolerance)


# --- parser unit tests -----------------------------------------------------

def test_named_and_keyword_colors():
    assert color('red') == (1, 0, 0, 1)
    assert color('  BLUE ') == (0, 0, 1, 1)
    assert color('none') == (0, 0, 0, 0)
    assert color('transparent') == (0, 0, 0, 0)
    assert color('') == (0, 0, 0, 0)
    assert color(None) == (0, 0, 0, 0)


def test_hex_rgb_and_rrggbb():
    assert color('#f00') == (1, 0, 0, 1)
    assert color('#F00') == (1, 0, 0, 1)
    assert color('#ff0000') == (1, 0, 0, 1)
    assert color('#00ff00') == (0, 1, 0, 1)
    assert color('#0000ff') == (0, 0, 1, 1)
    assert color('#336699') == pytest.approx((0.2, 0.4, 0.6, 1), abs=1e-3)


def test_hex_rgba_short_alpha():
    # 0x8 duplicated gives 0x88 = 136, alpha 136 / 255.
    r, g, b, a = color('#f008')
    assert (r, g, b) == (1, 0, 0)
    assert a == pytest.approx(136 / 255, abs=1e-12)
    assert color('#f00f')[3] == 1
    assert color('#f000')[3] == 0


def test_hex_rrggbbaa_long_alpha():
    assert color('#ff000080')[3] == pytest.approx(128 / 255, abs=1e-12)
    assert color('#ff0000ff')[3] == 1
    assert color('#ff000000')[3] == 0
    channels = color('#33669980')
    assert channels[:3] == pytest.approx((0.2, 0.4, 0.6), abs=1e-3)
    assert channels[3] == pytest.approx(128 / 255, abs=1e-12)


def test_legacy_rgb_comma_syntax_is_kept():
    assert color('rgb(255,0,0)') == (1, 0, 0, 1)
    assert color('rgb(255, 0, 0)') == (1, 0, 0, 1)
    assert color('rgb(100%, 0%, 0%)') == (1, 0, 0, 1)
    assert color('rgb(50%,50%,50%)')[:3] == pytest.approx((.5, .5, .5))
    assert color('rgba(255,0,0,0.5)') == (1, 0, 0, .5)
    assert color('rgba(255, 0, 0, 0)') == (1, 0, 0, 0)
    assert color('rgba(100%,0%,0%,1)') == (1, 0, 0, 1)


def test_legacy_rgba_percentage_alpha():
    # Regression: percentage alpha used to be multiplied by 255 instead of
    # being divided by 100.
    assert color('rgba(255,0,0,50%)') == (1, 0, 0, .5)
    assert color('rgba(100%, 0%, 0%, 25%)') == (1, 0, 0, .25)
    assert color('rgba(100%, 0%, 0%, 100%)') == (1, 0, 0, 1)


def test_modern_space_rgb_syntax():
    assert color('rgb(255 0 0)') == (1, 0, 0, 1)
    assert color('rgb( 255 0 0 )') == (1, 0, 0, 1)
    assert color('rgb(100% 0% 0%)') == (1, 0, 0, 1)
    assert color('rgb(50% 50% 50%)')[:3] == pytest.approx((.5, .5, .5))


def test_modern_slash_alpha_syntax():
    assert color('rgb(255 0 0 / 0.5)') == (1, 0, 0, .5)
    assert color('rgb(255 0 0/0.5)') == (1, 0, 0, .5)
    assert color('rgb(255 0 0 / 50%)') == (1, 0, 0, .5)
    assert color('rgb(100% 0% 0% / 25%)') == (1, 0, 0, .25)
    assert color('rgba(255 0 0 / 1)') == (1, 0, 0, 1)
    assert color('rgb(255 0 0 / 0)')[3] == 0


def test_channels_are_clamped_to_range():
    # Out of range number and percentage channels clamp to [0, 1].
    assert color('rgb(300, -10, 0)')[:3] == (1, 0, 0)
    assert color('rgb(110%, -5%, 0%)')[:3] == (1, 0, 0)
    assert color('rgb(255 255 255 / 1.5)')[3] == 1
    assert color('rgb(255 255 255 / -0.5)')[3] == 0


def test_invalid_colors_never_raise():
    for bad in (
            'boguscolor', '#12345', '#gggggg', 'rgb()', 'rgb(1 2)',
            'rgb(255, 0, 0 / 0.5)', 'rgb(255 0%, 0)',
            'rgba(255,0,0,0.5,1)', 'rgb(none none none)',
            'hsl(0 100% 50%)', 'rgb(,,)'):
        assert color(bad) == INVALID_COLOR, bad


def test_color_alpha_combines_with_external_opacity_once():
    # External opacity multiplies the color alpha, it is never applied twice
    # and never dropped.
    assert color('rgba(255,0,0,0.5)', 0.5) == (1, 0, 0, .25)
    assert color('#ff000080', 0.5)[3] == pytest.approx(
        (128 / 255) * .5, abs=1e-12)
    assert color('red', 0.25) == (1, 0, 0, .25)
    assert color('transparent', 0.5) == (0, 0, 0, 0)


@given(value=st.integers(0, 255))
def test_hex_and_functional_notation_are_equivalent(value):
    hex_color = f'#{value:02x}{value:02x}{value:02x}'
    rgb_color = f'rgb({value},{value},{value})'
    space_color = f'rgb({value} {value} {value})'
    expected = value / 255
    for notation in (hex_color, rgb_color, space_color):
        r, g, b, a = color(notation)
        assert r == g == b == pytest.approx(expected, abs=1e-12)
        assert a == 1


@given(value=st.integers(0, 255), alpha=st.integers(0, 255))
def test_alpha_notation_equivalence(value, alpha):
    hex_color = f'#{value:02x}{value:02x}{value:02x}{alpha:02x}'
    factor = alpha / 255
    rgba_comma = f'rgba({value},{value},{value},{factor})'
    rgba_slash = f'rgb({value} {value} {value} / {factor})'
    channel_percent = value / 255 * 100
    rgba_percent = (
        f'rgba({channel_percent}%, {channel_percent}%, '
        f'{channel_percent}%, {factor * 100}%)')
    expected_alpha = alpha / 255
    for notation in (hex_color, rgba_comma, rgba_slash, rgba_percent):
        r, g, b, a = color(notation)
        assert r == g == b == pytest.approx(value / 255, abs=1e-9)
        # Functional notations round-trip through decimals: allow the
        # half-step quantization error of an 8-bit alpha channel.
        assert a == pytest.approx(expected_alpha, abs=0.5 / 255)


# --- rendering tests: equivalent notations, real pixels --------------------

EQUIVALENT_RED_HALF = (
    '#ff000080',
    'rgba(255,0,0,0.5019607843137255)',
    'rgba(100%, 0%, 0%, 50.19607843137255%)',
    'rgb(255 0 0 / 0.5019607843137255)',
    'rgb(100% 0% 0% / 50.19607843137255%)',
)

# The 4-digit hex form quantizes alpha differently (0x88 = 136/255), check
# it against its own equivalent notations.
EQUIVALENT_RED_SHORT = (
    '#f008',
    '#ff000088',
    'rgba(255,0,0,0.5333333333333333)',
    'rgb(255 0 0 / 53.333333333333336%)',
)


@pytest.mark.parametrize('notation', EQUIVALENT_RED_HALF)
def test_equivalent_notations_render_identical_fill(notation):
    reference = center_pixel(rect_svg(EQUIVALENT_RED_HALF[0]))
    approx_pixel(center_pixel(rect_svg(notation)), reference)
    # Alpha 128/255 rounds to 128 in the 8-bit PNG channel.
    approx_pixel(reference, (255, 0, 0, 128))


@pytest.mark.parametrize('notation', EQUIVALENT_RED_SHORT)
def test_short_hex_alpha_renders_identical_to_long_form(notation):
    reference = center_pixel(rect_svg(EQUIVALENT_RED_SHORT[0]))
    approx_pixel(center_pixel(rect_svg(notation)), reference)
    # 0x88 = 136 in the 8-bit alpha channel.
    approx_pixel(reference, (255, 0, 0, 136))


def test_opaque_hex_equivalence_renders_identical():
    notations = ('#f00', '#ff0000', 'rgb(255,0,0)', 'rgb(255 0 0)',
                 'rgb(100%, 0%, 0%)', 'red')
    reference = center_pixel(rect_svg(notations[0]))
    for notation in notations[1:]:
        approx_pixel(center_pixel(rect_svg(notation)), reference)
    assert reference == (255, 0, 0, 255)


def test_inline_style_and_presentation_attribute_match():
    attr_svg = rect_svg('rgb(255 0 0 / 50%)')
    style_svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
      <rect width="10" height="10" style="fill: rgb(255 0 0 / 50%)"/>
    </svg>'''
    approx_pixel(center_pixel(style_svg), center_pixel(attr_svg))


def test_stroke_uses_same_parser_and_alpha():
    def stroke_svg(paint):
        return f'''<svg xmlns="http://www.w3.org/2000/svg"
            width="20" height="20">
          <rect x="2" y="2" width="16" height="16" fill="none"
                stroke="{paint}" stroke-width="16"/>
        </svg>'''
    reference = center_pixel(stroke_svg('#ff000080'))
    for notation in (
            'rgba(255,0,0,0.5019607843137255)',
            'rgb(255 0 0 / 50.19607843137255%)',
            'rgba(100%,0%,0%,50.19607843137255%)'):
        approx_pixel(center_pixel(stroke_svg(notation)), reference)
    approx_pixel(reference, (255, 0, 0, 128))


def test_gradient_stops_accept_alpha_notations():
    def gradient_svg(stop0, stop1):
        return f'''<svg xmlns="http://www.w3.org/2000/svg"
            width="40" height="10">
          <defs>
            <linearGradient id="g" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stop-color="{stop0}"/>
              <stop offset="1" stop-color="{stop1}"/>
            </linearGradient>
          </defs>
          <rect width="40" height="10" fill="url(#g)"/>
        </svg>'''
    variants = (
        ('#0000ff80', '#ffffffff'),
        ('rgba(0,0,255,0.5019607843137255)', 'rgb(255 255 255 / 1)'),
        ('rgb(0 0 255 / 50.19607843137255%)', '#ffffff'),
    )
    images = [render_pixels(gradient_svg(a, b), (40, 10))
              for a, b in variants]
    reference = images[0]
    for image in images[1:]:
        assert list(image.get_flattened_data()) == \
            list(reference.get_flattened_data())
    # Sanity check on the gradient ends (interpolation bleeds one pixel at
    # the boundaries, so only assert on the dominant channel and alpha).
    left = reference.getpixel((0, 5))
    right = reference.getpixel((39, 5))
    assert left[2] == 255 and left[3] == pytest.approx(128, abs=2)
    assert right[:3] == pytest.approx((255, 255, 255), abs=4)
    assert right[3] > 250


def test_solid_gradient_stop_with_alpha_matches_flat_fill():
    # A single-stop gradient is a flat paint: its rendered pixel must match
    # a plain fill using an equivalent color notation.
    def single_stop_svg(stop_color):
        return f'''<svg xmlns="http://www.w3.org/2000/svg"
            width="10" height="10">
          <defs>
            <linearGradient id="g" x1="0" y1="0" x2="1" y2="0">
              <stop offset="0" stop-color="{stop_color}"/>
              <stop offset="1" stop-color="{stop_color}"/>
            </linearGradient>
          </defs>
          <rect width="10" height="10" fill="url(#g)"/>
        </svg>'''
    notations = (
        '#00ff0080',
        'rgba(0,255,0,0.5019607843137255)',
        'rgb(0 255 0 / 50.19607843137255%)',
        'rgba(0%,100%,0%,50.19607843137255%)',
    )
    pixels = [center_pixel(single_stop_svg(notation)) for notation in notations]
    for pixel in pixels[1:]:
        approx_pixel(pixel, pixels[0])
    assert pixels[0] == (0, 255, 0, 128)


def test_stop_color_alpha_combines_with_stop_opacity_once():
    # stop alpha 0.5 * stop-opacity 0.5 == 0.25, no double multiplication.
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="rgb(255 0 0 / 50%)"
                stop-opacity="0.5"/>
          <stop offset="1" stop-color="rgb(255 0 0 / 50%)"
                stop-opacity="0.5"/>
        </linearGradient>
      </defs>
      <rect width="10" height="10" fill="url(#g)"/>
    </svg>'''
    # 0.25 * 255 = 63.75 -> 64 after rounding in the PNG.
    assert center_pixel(svg)[3] == pytest.approx(64, abs=1)


def test_fill_opacity_combines_with_color_alpha_once():
    # rgba alpha 0.5 combined with fill-opacity 0.5 must give 0.25.
    combined = center_pixel(rect_svg(
        'rgba(255,0,0,0.5)', 'fill-opacity="0.5"'))
    assert combined[3] == pytest.approx(64, abs=1)
    assert combined[:3] == (255, 0, 0)

    # Same total alpha expressed only through fill-opacity on an opaque color.
    plain = center_pixel(rect_svg('red', 'fill-opacity="0.25"'))
    approx_pixel(combined, plain)


def test_stroke_opacity_combines_with_color_alpha_once():
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20">
      <rect x="2" y="2" width="16" height="16" fill="none"
            stroke="rgb(255 0 0 / 50%)" stroke-width="16"
            stroke-opacity="0.5"/>
    </svg>'''
    pixel = center_pixel(svg)
    assert pixel[:3] == (255, 0, 0)
    assert pixel[3] == pytest.approx(64, abs=1)


def test_group_opacity_combines_with_color_alpha_once():
    # Leaf rect (no children): group opacity is folded into fill opacity,
    # then multiplied once with the color alpha.
    leaf_svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
      <g opacity="0.5">
        <rect width="10" height="10" fill="rgb(255 0 0 / 50%)"/>
      </g>
    </svg>'''
    assert center_pixel(leaf_svg)[3] == pytest.approx(64, abs=1)

    # Group with children is rendered to an intermediate group and painted
    # with the group alpha: total alpha is still 0.5 * 0.5 = 0.25.
    group_svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">
      <g opacity="0.5">
        <rect width="5" height="10" fill="rgb(255 0 0 / 50%)"/>
        <rect x="5" width="5" height="10" fill="rgb(255 0 0 / 50%)"/>
      </g>
    </svg>'''
    assert center_pixel(group_svg)[3] == pytest.approx(64, abs=1)


def test_semi_transparent_overlap_uses_source_over_compositing():
    # Two half-transparent red rects overlap: the overlap alpha follows the
    # standard source-over formula a01 = a0 + a1 (1 - a0) = 0.75.
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">
      <rect x="0" width="15" height="10" fill="rgb(255 0 0 / 50%)"/>
      <rect x="5" width="15" height="10" fill="rgb(255 0 0 / 50%)"/>
    </svg>'''
    image = render_pixels(svg, (20, 10))
    assert image.getpixel((2, 5))[3] == pytest.approx(128, abs=1)
    overlap = image.getpixel((10, 5))
    assert overlap[:3] == (255, 0, 0)
    assert overlap[3] == pytest.approx(191, abs=1)  # 0.75 * 255


def test_invalid_color_does_not_break_the_rest_of_the_document():
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="20" height="10">
      <rect x="0" width="10" height="10" fill="hsl(0 100% 50%)"/>
      <rect x="10" width="10" height="10" fill="rgb(255 0 0)"/>
    </svg>'''
    image = render_pixels(svg, (20, 10))
    # Historical fallback: invalid color paints opaque black.
    assert image.getpixel((5, 5)) == (0, 0, 0, 255)
    # The sibling shape still renders correctly.
    assert image.getpixel((15, 5)) == (255, 0, 0, 255)


def test_repeated_exports_are_byte_identical():
    svg = rect_svg('rgb(255 0 0 / 50%)').encode()
    first = svg2png(bytestring=svg, output_width=20, output_height=20)
    for _ in range(5):
        assert svg2png(
            bytestring=svg, output_width=20, output_height=20) == first


def test_presentation_attribute_inline_style_and_gradient_together():
    # One document exercising all three color entry points at once.
    svg = '''<svg xmlns="http://www.w3.org/2000/svg" width="30" height="10">
      <defs>
        <linearGradient id="g" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="#00ff0080"/>
          <stop offset="1" stop-color="rgb(0 255 0 / 50.19607843137255%)"/>
        </linearGradient>
      </defs>
      <rect x="0" width="10" height="10" fill="#ff000080"/>
      <rect x="10" width="10" height="10"
            style="fill: rgb(255 0 0 / 50.19607843137255%)"/>
      <rect x="20" width="10" height="10" fill="url(#g)"/>
    </svg>'''
    image = render_pixels(svg, (30, 10))
    assert image.getpixel((5, 5)) == (255, 0, 0, 128)
    assert image.getpixel((15, 5)) == (255, 0, 0, 128)
    assert image.getpixel((25, 5)) == (0, 255, 0, 128)
