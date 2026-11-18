"""
Tests for CSS color parsing and alpha composition.

These tests cover the color syntaxes supported by CairoSVG, check that
equivalent syntaxes render the same pixels, and check that color alpha,
``fill-opacity``/``stroke-opacity``/``stop-opacity`` and group ``opacity``
compose exactly once.

"""

import io

import pytest
from PIL import Image

from . import svg2pdf, svg2png
from .colors import color, parse_opacity


def render(svg, background=None):
    """Render an SVG string and return it as a Pillow RGBA image."""
    kwargs = {'bytestring': svg.encode('utf-8')}
    if background is not None:
        kwargs['background_color'] = background
    png = svg2png(**kwargs)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def pixel(svg, position=(20, 20), background=None):
    """Render an SVG string and return one pixel."""
    return render(svg, background).getpixel(position)


def document(body, width=40, height=40, attributes=''):
    """Wrap ``body`` in a minimal SVG document."""
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
        f'height="{height}" {attributes}>{body}</svg>')


def rgba_tuple(rgba):
    """Convert an ``(r, g, b, a)`` float tuple to 8-bit values."""
    return tuple(round(value * 255) for value in rgba)


@pytest.mark.parametrize('string, expected', (
    ('#f00', (255, 0, 0, 255)),
    ('#F00', (255, 0, 0, 255)),
    ('#ff0000', (255, 0, 0, 255)),
    ('#f00f', (255, 0, 0, 255)),
    ('#ff0000ff', (255, 0, 0, 255)),
    ('#f008', (255, 0, 0, 136)),
    ('#ff000080', (255, 0, 0, 128)),
    ('#abcd', (170, 187, 204, 221)),
    ('#aabbccdd', (170, 187, 204, 221)),
    ('red', (255, 0, 0, 255)),
    ('transparent', (0, 0, 0, 0)),
    ('none', (0, 0, 0, 0)),
    ('rgb(255,0,0)', (255, 0, 0, 255)),
    ('rgb(255, 0, 0)', (255, 0, 0, 255)),
    ('RGB(255, 0, 0)', (255, 0, 0, 255)),
    ('rgb(100%, 0%, 0%)', (255, 0, 0, 255)),
    ('rgba(255,0,0,0.5)', (255, 0, 0, 128)),
    ('rgba(255,0,0,50%)', (255, 0, 0, 128)),
    ('rgba(100%,0%,0%,0.5)', (255, 0, 0, 128)),
    ('rgb(255 0 0)', (255, 0, 0, 255)),
    ('rgb(100% 0% 0%)', (255, 0, 0, 255)),
    ('rgb(255 0 0 / 0.5)', (255, 0, 0, 128)),
    ('rgb(255 0 0 / 50%)', (255, 0, 0, 128)),
    ('rgb(100% 0% 0% / 50%)', (255, 0, 0, 128)),
    ('rgb(50% 0 0)', (128, 0, 0, 255)),
    ('  #f00  ', (255, 0, 0, 255)),
    ('rgb(300, -20, 0)', (255, 0, 0, 255)),
    ('rgb(255 0 0 / 200%)', (255, 0, 0, 255)),
    ('rgb(255 0 0 / -1)', (255, 0, 0, 0)),
))
def test_color_syntaxes(string, expected):
    """Supported syntaxes resolve to the expected 8-bit RGBA value."""
    assert rgba_tuple(color(string)) == expected


@pytest.mark.parametrize('string', (
    'notacolor',
    '#ggg',
    '#ff000',
    '#ff00000',
    'rgb(1,2)',
    'rgb(1,2,3,4,5)',
    'rgb(foo)',
    'rgb(255,,0,0)',
    'rgb(255 0 0 /)',
    'rgb(255 0 0 / 1 / 2)',
    'rgb(255, 0, 0 / 0.5)',
    'hsl(0, 100%, 50%)',
    'hsl(0 100% 50%)',
    'lab(50% 40 59.5)',
    'color(display-p3 1 0 0)',
    'rgb(float, 0, 0)',
))
def test_invalid_colors_fall_back_to_black(string):
    """Invalid colors use the legacy fallback and never raise."""
    assert color(string) == (0, 0, 0, 1)


def test_empty_color_is_transparent():
    """Falsy color values keep the historical transparent result."""
    assert color('') == (0, 0, 0, 0)
    assert color(None) == (0, 0, 0, 0)


def test_color_alpha_composes_with_passed_opacity():
    """The color's own alpha is multiplied by the given opacity once."""
    assert color('rgba(255, 0, 0, 0.5)', 0.5)[3] == pytest.approx(0.25)
    assert color('#ff000080', 0.5)[3] == pytest.approx(128 / 255 / 2)
    assert color('red', 0.5)[3] == pytest.approx(0.5)
    assert color('rgb(255 0 0 / 50%)', 0.5)[3] == pytest.approx(0.25)


@pytest.mark.parametrize('value, expected', (
    (None, 1),
    ('1', 1),
    ('0.5', 0.5),
    ('50%', 0.5),
    ('200%', 1),
    ('-1', 0),
    ('', 1),
    ('abc', 1),
    ('0.5 0.5', 1),
))
def test_parse_opacity(value, expected):
    """Opacity attributes accept numbers and percentages, and stay tolerant."""
    assert parse_opacity(value) == expected


FILL_SYNTAXES = (
    '#f00',
    '#ff0000',
    'red',
    'rgb(255,0,0)',
    'rgb(255 0 0)',
    'rgb(100%, 0%, 0%)',
    'rgb(100% 0% 0%)',
)

ALPHA_SYNTAXES = (
    '#ff000080',
    'rgba(255,0,0,0.5)',
    'rgb(255 0 0 / 0.5)',
    'rgb(255 0 0 / 50%)',
    'rgb(100% 0% 0% / 50%)',
)


@pytest.mark.parametrize('syntax', FILL_SYNTAXES)
def test_fill_equivalent_syntaxes(syntax):
    """Equivalent opaque fill syntaxes render identical pixels."""
    reference = pixel(document(
        '<rect width="40" height="40" fill="#ff0000"/>'))
    rendered = pixel(document(
        f'<rect width="40" height="40" fill="{syntax}"/>'))
    assert rendered == reference == (255, 0, 0, 255)


@pytest.mark.parametrize('syntax', ALPHA_SYNTAXES)
def test_fill_alpha_syntaxes(syntax):
    """Equivalent alpha fill syntaxes render identical pixels."""
    reference = pixel(document(
        '<rect width="40" height="40" fill="#ff000080"/>'))
    rendered = pixel(document(
        f'<rect width="40" height="40" fill="{syntax}"/>'))
    assert rendered == reference == (255, 0, 0, 128)


@pytest.mark.parametrize('syntax', ALPHA_SYNTAXES)
def test_style_declaration_alpha(syntax):
    """The same alpha syntaxes work in an inline ``style`` declaration."""
    reference = pixel(document(
        '<rect width="40" height="40" fill="#ff000080"/>'))
    rendered = pixel(document(
        f'<rect width="40" height="40" style="fill:{syntax}"/>'))
    assert rendered == reference == (255, 0, 0, 128)


@pytest.mark.parametrize('syntax', (
    '#008000', 'rgb(0,128,0)', 'rgb(0 128 0)', 'rgb(0% 50.1961% 0%)'))
def test_stroke_equivalent_syntaxes(syntax):
    """Equivalent stroke syntaxes render identical pixels."""
    svg = document(
        f'<line x1="5" y1="20" x2="35" y2="20" stroke="{syntax}" '
        'stroke-width="10"/>')
    assert pixel(svg) == (0, 128, 0, 255)


@pytest.mark.parametrize('stroke, opacity', (
    ('#008000', '0.5'),
    ('rgba(0,128,0,0.5)', '1'),
    ('rgb(0 128 0 / 50%)', '1'),
))
def test_stroke_alpha_and_stroke_opacity_compose(stroke, opacity):
    """Color alpha and ``stroke-opacity`` both apply, exactly once."""
    svg = document(
        f'<line x1="5" y1="20" x2="35" y2="20" stroke="{stroke}" '
        f'stroke-width="10" stroke-opacity="{opacity}"/>')
    assert pixel(svg) == (0, 128, 0, 128)


def test_background_color_alpha():
    """The ``background_color`` option accepts the same syntaxes."""
    for value in ('#ff000080', 'rgb(255 0 0 / 50%)'):
        png = svg2png(
            bytestring=document('').encode('utf-8'),
            background_color=value)
        image = Image.open(io.BytesIO(png)).convert('RGBA')
        assert image.getpixel((20, 20)) == (255, 0, 0, 128)


def test_fill_opacity_composes_with_color_alpha():
    """``fill-opacity`` multiplies the color alpha instead of replacing it."""
    svg = document(
        '<rect width="40" height="40" fill="#ff000080" '
        'fill-opacity="0.5"/>')
    assert pixel(svg) == (255, 0, 0, 64)


def test_element_opacity_applies_once():
    """Element ``opacity`` is applied once, even with a group or mask."""
    mask = (
        '<defs><mask id="m"><rect width="40" height="40" fill="#fff"/>'
        '</mask></defs>')
    filter_ = (
        '<defs><filter id="f"><feOffset dx="0" dy="0"/></filter></defs>')
    for extra in ('', mask, filter_):
        svg = document(
            extra + '<rect width="40" height="40" fill="#f00" '
            'opacity="0.5" ' +
            ('mask="url(#m)"' if extra is mask else
             'filter="url(#f)"' if extra is filter_ else '') + '/>')
        assert pixel(svg) == (255, 0, 0, 128)


def test_nested_group_opacity_multiplies():
    """Nested group opacities multiply, without double application."""
    svg = document(
        '<g opacity="0.5"><g opacity="0.5">'
        '<rect width="40" height="40" fill="#f00"/>'
        '</g></g>')
    assert pixel(svg) == (255, 0, 0, 64)


def test_group_opacity_composes_with_color_alpha():
    """Color alpha, fill-opacity and group opacity multiply."""
    svg = document(
        '<g opacity="0.5"><rect width="40" height="40" '
        'fill="rgba(255,0,0,0.5)" fill-opacity="0.5"/></g>')
    assert pixel(svg) == (255, 0, 0, 32)


def test_use_opacity_applies_once():
    """``<use>`` opacity matches the opacity of the referenced shape."""
    direct = pixel(document(
        '<rect width="40" height="40" fill="#f00" opacity="0.5"/>'))
    for body in (
        '<defs><rect id="r" width="40" height="40" fill="#f00"/></defs>'
        '<use href="#r" opacity="0.5"/>',
        '<defs><symbol id="r">'
        '<rect width="40" height="40" fill="#f00"/></symbol></defs>'
        '<use href="#r" opacity="0.5"/>',
    ):
        assert pixel(document(body)) == direct


def test_use_opacity_multiplies_with_fill_opacity():
    """``<use>`` opacity and the referenced ``fill-opacity`` multiply."""
    svg = document(
        '<defs><rect id="r" width="40" height="40" fill="#f00" '
        'fill-opacity="0.5"/></defs>'
        '<use href="#r" opacity="0.5"/>')
    assert pixel(svg) == (255, 0, 0, 64)


def test_nested_use_opacity_multiplies():
    """Nested ``<use>`` opacities multiply without double application."""
    svg = document(
        '<defs><rect id="r" width="40" height="40" fill="#f00"/>'
        '<g id="g"><use href="#r" opacity="0.5"/></g></defs>'
        '<use href="#g" opacity="0.5"/>')
    assert pixel(svg) == (255, 0, 0, 64)


def test_mask_alpha_applies_once():
    """A mask painted at 50% alpha halves the element alpha, exactly once."""
    svg = document(
        '<defs><mask id="m"><rect width="40" height="40" fill="#fff" '
        'fill-opacity="0.5"/></mask></defs>'
        '<rect width="40" height="40" fill="#f00" mask="url(#m)"/>')
    assert pixel(svg) == (255, 0, 0, 128)


def test_mask_referenced_twice_keeps_opacity():
    """Referencing a mask twice must not compound its opacity."""
    mask = (
        '<defs><mask id="m"><rect width="20" height="40" fill="#fff"/>'
        '</mask></defs>')
    svg = document(
        mask +
        '<rect width="20" height="40" fill="#f00" opacity="0.5" '
        'mask="url(#m)"/>'
        '<rect x="20" width="20" height="40" fill="#f00" opacity="0.5" '
        'mask="url(#m)"/>')
    image = render(svg)
    assert image.getpixel((10, 20)) == image.getpixel((30, 20)) == (
        255, 0, 0, 128)


def test_gradient_referenced_twice_keeps_opacity():
    """Referencing a gradient twice must not compound its stop alpha."""
    gradient = (
        '<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" '
        'x1="0" y1="0" x2="20" y2="0">'
        '<stop offset="0" stop-color="#f00" stop-opacity="0.5"/>'
        '<stop offset="1" stop-color="#f00" stop-opacity="0.5"/>'
        '</linearGradient></defs>')
    svg = document(
        gradient +
        '<rect width="20" height="40" fill="url(#g)"/>'
        '<rect x="20" width="20" height="40" fill="url(#g)"/>')
    image = render(svg)
    assert image.getpixel((10, 20)) == image.getpixel((30, 20)) == (
        255, 0, 0, 128)


def test_marker_color_alpha():
    """Markers accept the extended color syntaxes with alpha."""
    def marker(fill):
        return document(
            '<defs><marker id="m" markerWidth="12" markerHeight="12" '
            'refX="6" refY="6" markerUnits="userSpaceOnUse">'
            f'<circle cx="6" cy="6" r="5" fill="{fill}"/>'
            '</marker></defs>'
            '<path d="M 20 20 L 60 20" stroke="#000" stroke-width="1" '
            'marker-start="url(#m)"/>', width=80, height=40)

    reference = render(marker('#ff000080'))
    for syntax in ('rgba(255,0,0,0.5)', 'rgb(255 0 0 / 50%)'):
        assert render(marker(syntax)).tobytes() == reference.tobytes()
    assert reference.getpixel((20, 16)) == (255, 0, 0, 128)


@pytest.mark.parametrize('color_syntax', (
    '#ff000080', 'rgba(255,0,0,0.5)', 'rgb(255 0 0 / 50%)'))
@pytest.mark.parametrize('stop_opacity', ('1', '0.5', '50%'))
def test_gradient_stop_color_alpha(color_syntax, stop_opacity):
    """Gradient stop color alpha composes once with ``stop-opacity``."""
    svg = document(
        '<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" '
        'x1="0" y1="0" x2="40" y2="0">'
        f'<stop offset="0" stop-color="{color_syntax}" '
        f'stop-opacity="{stop_opacity}"/>'
        f'<stop offset="1" stop-color="{color_syntax}" '
        f'stop-opacity="{stop_opacity}"/>'
        '</linearGradient></defs>'
        '<rect width="40" height="40" fill="url(#g)"/>')
    expected_alpha = 128 if stop_opacity == '1' else 64
    assert pixel(svg) == (255, 0, 0, expected_alpha)


def test_gradient_equivalent_stops_identical():
    """Equivalent gradient stop syntaxes produce identical images."""
    def gradient(stop_color):
        return document(
            '<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" '
            'x1="0" y1="0" x2="40" y2="0">'
            f'<stop offset="0" stop-color="{stop_color}" stop-opacity="0.5"/>'
            f'<stop offset="1" stop-color="{stop_color}" stop-opacity="0.5"/>'
            '</linearGradient></defs>'
            '<rect width="40" height="40" fill="url(#g)"/>')

    reference = render(gradient('#ff000080')).tobytes()
    for syntax in ('rgba(255,0,0,0.5)', 'rgb(255 0 0 / 50%)'):
        assert render(gradient(syntax)).tobytes() == reference
    assert pixel(gradient('#ff000080')) == (255, 0, 0, 64)


def test_gradient_with_fill_opacity():
    """``fill-opacity`` applies once on top of the gradient stop alpha."""
    svg = document(
        '<defs><linearGradient id="g" gradientUnits="userSpaceOnUse" '
        'x1="0" y1="0" x2="40" y2="0">'
        '<stop offset="0" stop-color="rgb(255 0 0 / 50%)"/>'
        '<stop offset="1" stop-color="rgb(255 0 0 / 50%)"/>'
        '</linearGradient></defs>'
        '<rect width="40" height="40" fill="url(#g)" fill-opacity="0.5"/>')
    assert pixel(svg) == (255, 0, 0, 64)


def test_translucent_overlap():
    """Two 50% layers over each other give 75% alpha, as in CSS."""
    svg = document(
        '<rect width="30" height="40" fill="rgba(255,0,0,0.5)"/>'
        '<rect x="10" width="30" height="40" fill="rgb(255 0 0 / 50%)"/>')
    image = render(svg)
    assert image.getpixel((5, 20)) == (255, 0, 0, 128)
    assert image.getpixel((20, 20)) == (255, 0, 0, 192)
    assert image.getpixel((35, 20)) == (255, 0, 0, 128)


def test_repeated_export_is_stable():
    """Rendering the same document twice returns identical pixels."""
    source = document(
        '<rect width="20" height="40" fill="rgba(255,0,0,0.5)"/>'
        '<rect x="20" width="20" height="40" fill="rgb(0 0 255 / 50%)"/>')
    first = svg2png(bytestring=source.encode('utf-8'))
    second = svg2png(bytestring=source.encode('utf-8'))
    assert first == second


def test_repeated_pattern_reference_same_opacity():
    """Referencing a pattern twice does not compound its opacity."""
    svg = document(
        '<defs><pattern id="p" width="10" height="10" '
        'patternUnits="userSpaceOnUse">'
        '<rect width="10" height="10" fill="#0000ff"/></pattern></defs>'
        '<rect width="20" height="20" fill="url(#p)" fill-opacity="0.5"/>'
        '<rect x="20" y="20" width="20" height="20" fill="url(#p)" '
        'fill-opacity="0.5"/>')
    image = render(svg)
    assert image.getpixel((5, 5)) == image.getpixel((25, 25)) == (
        0, 0, 255, 128)


def test_invalid_color_does_not_break_document():
    """A bad color only loses its own paint, not the whole document."""
    svg = document(
        '<rect width="20" height="40" style="fill:rgb(1,2)"/>'
        '<rect x="20" width="20" height="40" fill="#00ff00"/>'
        '<rect y="30" width="20" height="10" fill="notacolor"/>'
        '<circle cx="30" cy="35" r="4" fill="hsl(0,100%,50%)"/>')
    image = render(svg)
    assert image.getpixel((10, 10)) == (0, 0, 0, 255)
    assert image.getpixel((30, 10)) == (0, 255, 0, 255)
    assert image.getpixel((10, 35)) == (0, 0, 0, 255)


def test_invalid_opacity_does_not_break_document():
    """Bad opacity values fall back to 1 and do not raise."""
    svg = document(
        '<g opacity="not-a-number">'
        '<rect width="20" height="40" fill="#ff0000" '
        'fill-opacity="nope"/></g>'
        '<rect x="20" width="20" height="40" fill="#00ff00"/>')
    image = render(svg)
    assert image.getpixel((10, 20)) == (255, 0, 0, 255)
    assert image.getpixel((30, 20)) == (0, 255, 0, 255)


def test_current_color_is_case_insensitive():
    """The ``currentColor`` keyword works in any case, in attr and style."""
    wrapper = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" '
        'color="#00ff00">{}</svg>')
    for attributes in (
        'fill="currentColor"',
        'fill="currentcolor"',
        'style="fill:currentColor"',
        'style="fill:CURRENTCOLOR"',
    ):
        png = svg2png(bytestring=wrapper.format(
            f'<rect width="40" height="40" {attributes}/>').encode('utf-8'))
        image = Image.open(io.BytesIO(png)).convert('RGBA')
        assert image.getpixel((20, 20)) == (0, 255, 0, 255)


def test_extended_colors_in_other_formats():
    """Vector output formats accept the extended color syntaxes too."""
    svg = document(
        '<rect width="20" height="20" fill="rgb(255 0 0 / 50%)"/>'
        '<rect x="20" width="20" height="20" fill="#00ff0080"/>')
    assert svg2pdf(bytestring=svg.encode('utf-8')).startswith(b'%PDF')


def test_presentation_attribute_and_style_precedence():
    """Inline styles override presentation attributes, both with alpha."""
    svg = document(
        '<rect width="40" height="40" fill="#00ff00" '
        'style="fill:rgb(255 0 0 / 50%)"/>')
    assert pixel(svg) == (255, 0, 0, 128)
