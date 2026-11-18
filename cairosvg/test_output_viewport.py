"""Regression tests for the PNG output size and the effective viewport.

CairoSVG resolves three different times a size is involved when converting an
SVG to PNG:

* the size declared by the document (``width``/``height`` attributes, possibly
  with units, or the ``viewBox`` as a fallback);
* the output size requested through ``output_width``, ``output_height`` and
  ``scale``, in output pixels;
* the whole-pixel size an ``ImageSurface`` can actually store.

This module checks that those three agree: the produced file has the announced
pixel size, its four edges are actually painted (no partially transparent last
row or column) and the document content is laid out in the stored viewport, not
in a fractional one.

The fixtures are small, font-free and use plain colors. Every checked pixel is
chosen by analytic geometry so that it is never on an antialiased edge.
"""

import io

import cairocffi as cairo
import pytest
from PIL import Image

from . import svg2png, svg2svg
from .surface import PNGSurface

# Colors used by the fixtures below, as (r, g, b, a) tuples.
TOP_LEFT = (0, 192, 0, 255)
TOP_RIGHT = (0, 0, 192, 255)
BOTTOM_LEFT = (192, 192, 0, 255)
BOTTOM_RIGHT = (0, 192, 192, 255)
CROSS = (16, 16, 16, 255)


def frame(width='20', height='20'):
    """Return a 20x20 unit square with a non-zero viewBox origin.

    The square is filled with a solid color and has:

    * a distinct 4x4 corner mark in each corner,
    * a 2-unit-wide line through its center, both horizontally and
      vertically.

    The viewBox origin is ``(7, 3)``: the content must be translated by
    ``(-7, -3)`` after scaling, so a misplaced origin is immediately visible
    in the corner colors.

    """
    width, height = str(width), str(height)
    return f'''\
<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}"
     viewBox="7 3 20 20">
  <rect x="7" y="3" width="20" height="20" fill="#c00000"/>
  <rect x="7" y="3" width="4" height="4" fill="#00c000"/>
  <rect x="23" y="3" width="4" height="4" fill="#0000c0"/>
  <rect x="7" y="19" width="4" height="4" fill="#c0c000"/>
  <rect x="23" y="19" width="4" height="4" fill="#00c0c0"/>
  <rect x="16" y="3" width="2" height="20" fill="#101010"/>
  <rect x="7" y="12" width="20" height="2" fill="#101010"/>
</svg>'''.encode()


def solid(width='20', height='20', viewbox=None):
    """Return a document fully covered by one flat rectangle.

    ``viewbox`` defaults to ``0 0 <width> <height>`` when ``width`` and
    ``height`` are plain numbers, so that the rectangle exactly covers the
    viewport.

    """
    if viewbox is None:
        viewbox = f'0 0 {width} {height}'
    x, y, view_width, view_height = viewbox.split()
    return f'''\
<svg xmlns="http://www.w3.org/2000/svg" width="{width}"
     height="{height}" viewBox="{viewbox}">
  <rect x="{x}" y="{y}" width="{view_width}" height="{view_height}"
        fill="#336699"/>
</svg>'''.encode()


def render(svg, **options):
    """Render ``svg`` to a PIL image."""
    png = svg2png(svg, **options)
    return Image.open(io.BytesIO(png)).convert('RGBA')


def border_pixels(image):
    """Return the pixels of the four edges of ``image``."""
    width, height = image.size
    return (
        [image.getpixel((x, 0)) for x in range(width)] +
        [image.getpixel((x, height - 1)) for x in range(width)] +
        [image.getpixel((0, y)) for y in range(height)] +
        [image.getpixel((width - 1, y)) for y in range(height)])


def corner_and_center_pixels(image):
    """Return the four corner marks and the center of ``frame()`` output.

    The probes are placed inside the marks, away from any antialiased edge,
    so that rounding the surface size up or down by one pixel cannot make the
    pixels land on a border.

    """
    width, height = image.size
    inset_x = max(1, round(width / 10))
    inset_y = max(1, round(height / 10))
    return [
        image.getpixel((inset_x, inset_y)),
        image.getpixel((width - 1 - inset_x, inset_y)),
        image.getpixel((inset_x, height - 1 - inset_y)),
        image.getpixel((width - 1 - inset_x, height - 1 - inset_y)),
        image.getpixel(((width - 1) // 2, (height - 1) // 2)),
    ]


@pytest.mark.parametrize('options, expected_size', (
    ({}, (20, 20)),
    ({'output_width': 25.5}, (26, 26)),
    ({'output_width': 37.8}, (38, 38)),
    ({'output_width': 101}, (101, 101)),
    ({'output_height': 25.5}, (26, 26)),
    ({'scale': 0.95}, (19, 19)),
    ({'scale': 2.71}, (54, 54)),
    ({'dpi': 150}, (20, 20)),
    ({'output_width': 40, 'output_height': 40}, (40, 40)),
))
def test_output_size_and_geometry(options, expected_size):
    """The file size is the announced one and the viewBox geometry holds.

    The four corner marks and the center cross must be found at the corners
    and center of the produced image, whatever the requested size and the
    way it was rounded.

    """
    image = render(frame(), **options)

    assert image.size == expected_size
    assert corner_and_center_pixels(image) == [
        TOP_LEFT, TOP_RIGHT, BOTTOM_LEFT, BOTTOM_RIGHT, CROSS]


@pytest.mark.parametrize('options, expected_size', (
    ({}, (20, 20)),
    ({'output_width': 25.5}, (26, 26)),
    ({'output_width': 37.8}, (38, 38)),
    ({'output_width': 101}, (101, 101)),
    ({'output_height': 25.5}, (26, 26)),
    ({'scale': 0.95}, (19, 19)),
    ({'scale': 2.71}, (54, 54)),
    ({'dpi': 150}, (20, 20)),
    ({'output_width': 40, 'output_height': 40}, (40, 40)),
))
def test_output_edges_are_fully_painted(options, expected_size):
    """No transparent line is left along the edge of the image.

    A flat rectangle covers the whole viewport: any edge pixel that is not
    fully opaque is the partially covered last row or column caused by a
    fractional viewport drawn on an integer-sized surface.

    """
    image = render(solid(), **options)

    assert image.size == expected_size
    assert all(pixel == (51, 102, 153, 255) for pixel in border_pixels(image))


def test_physical_units_are_scaled_by_dpi():
    """Physical source units are converted with ``dpi`` before rounding."""
    for width, expected in (
            ('25.4mm', (96, 96)), ('2.54cm', (96, 96)), ('1in', (96, 96)),
            ('72pt', (96, 96)), ('6pc', (96, 96)), ('96px', (96, 96)),
            ('5mm', (19, 19)), ('6.35mm', (24, 24))):
        image = render(frame(width, width))
        assert image.size == expected, width
        # The (zero) viewBox origin stays consistent with the physical size.
        assert corner_and_center_pixels(image)[0] == TOP_LEFT

    assert render(frame('1in', '1in'), dpi=72).size == (72, 72)
    assert render(frame('1in', '1in'), dpi=150).size == (150, 150)
    assert render(frame('10mm', '10mm'), dpi=300).size == (118, 118)


def test_physical_units_do_not_leave_partial_edges():
    """Fractional physical sizes still paint the whole edge."""
    for width in ('5mm', '6.35mm', '10mm', '25.4mm'):
        image = render(solid(width, width, viewbox='0 0 20 20'), dpi=96)
        assert all(
            pixel == (51, 102, 153, 255) for pixel in border_pixels(image)), (
                width)


def test_physical_units_and_viewbox_do_not_override_each_other():
    """The viewBox keeps its origin and size whatever the physical size."""
    # 25.4mm at the default 96 dpi is exactly 96 pixels: the 20-unit
    # viewBox is scaled by 4.8, and its (7, 3) origin is translated away.
    image = render(frame('25.4mm', '25.4mm'))
    assert image.size == (96, 96)
    assert corner_and_center_pixels(image) == [
        TOP_LEFT, TOP_RIGHT, BOTTOM_LEFT, BOTTOM_RIGHT, CROSS]
    assert image.size == (96, 96)
    assert corner_and_center_pixels(image) == [
        TOP_LEFT, TOP_RIGHT, BOTTOM_LEFT, BOTTOM_RIGHT, CROSS]


def test_single_output_dimension_keeps_ratio():
    """A lone ``output_width``/``output_height`` keeps the source ratio."""
    # Source ratio 20:10 = 2:1. The stored dimension is the requested one,
    # and the other one is derived from it so the ratio is not distorted by
    # rounding both dimensions independently.
    image = render(solid('20', '10'), output_width=40)
    assert image.size == (40, 20)

    image = render(solid('20', '10'), output_height=40)
    assert image.size == (80, 40)

    # 25.5 -> 26 stored pixels; 26 / 2 = 13.
    image = render(solid('20', '10'), output_width=25.5)
    assert image.size == (26, 13)

    # 13.7 -> 14 stored pixels; 14 * 2 = 28.
    image = render(solid('20', '10'), output_height=13.7)
    assert image.size == (28, 14)


def test_both_output_dimensions_are_used_as_is():
    """Both ``output_width`` and ``output_height`` are honored literally."""
    # The source ratio (2:1) is not preserved: this is the interface
    # contract, each requested dimension is used as requested.
    image = render(solid('20', '10'), output_width=40, output_height=40)
    assert image.size == (40, 40)

    # The default preserveAspectRatio letterboxes the content (transparent
    # top and bottom bands) but the file itself is 40x40.
    assert image.getpixel((0, 0)) == (0, 0, 0, 0)
    assert image.getpixel((20, 20)) == (51, 102, 153, 255)

    # A background color fills the letterboxed bands.
    image = render(
        solid('20', '10'), output_width=40, output_height=40,
        background_color='white')
    assert image.getpixel((0, 0)) == (255, 255, 255, 255)
    assert image.getpixel((20, 20)) == (51, 102, 153, 255)

    # A matching aspect ratio fills the whole surface.
    image = render(solid('20', '10'), output_width=40, output_height=20)
    assert image.size == (40, 20)
    assert all(pixel == (51, 102, 153, 255) for pixel in border_pixels(image))


@pytest.mark.parametrize('width, expected_size', (
    (0.4, None), (0.49, None), (0.5, None), (0.51, (1, 1)), (1, (1, 1)),
    (1.4, (1, 1)), (1.5, (2, 2)), (2, (2, 2)),
))
def test_small_output_sizes(width, expected_size):
    """A legal requested size never produces an empty or partial file.

    A requested dimension that rounds to zero pixels cannot be stored, and
    the existing ``ValueError`` contract is kept. A dimension that rounds to
    at least one pixel must give a fully painted image.

    """
    if expected_size is None:
        with pytest.raises(ValueError):
            svg2png(frame(), output_width=width)
    else:
        image = render(solid(), output_width=width)
        assert image.size == expected_size
        assert all(
            pixel == (51, 102, 153, 255) for pixel in border_pixels(image))


def test_smallest_legal_output_for_extreme_ratios():
    """A valid width gives at least one pixel of height."""
    # 1000:1 source. Asking for a single pixel wide still produces a
    # non-empty image instead of silently dropping the height to zero.
    svg = b'''\
<svg xmlns="http://www.w3.org/2000/svg" width="1000" height="1"
     viewBox="0 0 1000 1">
  <rect width="1000" height="1" fill="#336699"/>
</svg>'''
    image = render(svg, output_width=1)
    assert image.size == (1, 1)
    assert image.getpixel((0, 0)) == (51, 102, 153, 255)


@pytest.mark.parametrize('option, value', (
    ('output_width', 0),
    ('output_width', -1),
    ('output_width', float('nan')),
    ('output_width', float('inf')),
    ('output_height', 0),
    ('output_height', -2),
    ('output_height', float('nan')),
    ('scale', 0),
    ('scale', -1),
    ('scale', float('nan')),
    ('scale', float('inf')),
))
def test_invalid_sizes_are_rejected(option, value):
    """Invalid output sizes raise the historic ``ValueError``.

    No file must be written, so a caller checking the file existence after an
    exception never sees a "successful" empty image.

    """
    if option == 'scale':
        # A zero or negative source size also reaches the same check.
        options = {option: value}
    else:
        options = {option: value}
    with pytest.raises(ValueError):
        svg2png(frame(), **options)


def test_invalid_size_writes_no_file(tmp_path):
    """A rejected size must not leave a partial output file behind."""
    path = tmp_path / 'output.png'
    with pytest.raises(ValueError):
        svg2png(frame(), output_width=0, write_to=str(path))
    assert not path.exists()


@pytest.mark.parametrize('options, expected_size', (
    ({}, (24, 16)),
    ({'output_width': 25.5}, (26, 17)),
    ({'output_height': 13.7}, (21, 14)),
    ({'scale': 1.37}, (33, 22)),
    ({'output_width': 37}, (37, 25)),
    ({'output_width': 0.51}, (1, 1)),
))
def test_non_square_edges(options, expected_size):
    """Non-square sources get a fully painted edge too."""
    svg = b'''\
<svg xmlns="http://www.w3.org/2000/svg" width="24" height="16"
     viewBox="-5 -7 24 16">
  <rect x="-5" y="-7" width="24" height="16" fill="#336699"/>
</svg>'''
    image = render(svg, **options)
    assert image.size == expected_size
    assert all(pixel == (51, 102, 153, 255) for pixel in border_pixels(image))


def test_repeated_conversions_are_identical():
    """The same input gives byte-identical output, call after call."""
    for options in ({}, {'output_width': 37.8}, {'output_width': 25.5}):
        first = svg2png(frame(), **options)
        second = svg2png(frame(), **options)
        assert first == second


def test_low_level_surface_attributes():
    """The low-level surface reports the size it actually stored."""
    from .parser import Tree

    for output_width, expected in (
            (None, (20, 20)), (37.8, (38, 38)), (25.5, (26, 26))):
        surface = PNGSurface(
            Tree(bytestring=frame()), None, 96, None, None, None, 1,
            output_width, None)
        assert (surface.width, surface.height) == expected
        assert cairo.SurfacePattern(surface.cairo)


def test_vector_output_is_unchanged_by_the_viewport_fix():
    """The viewport fix only affects the raster surface pixel grid.

    Vector formats store exact sizes and must keep their historical output.

    """
    svg = b'''\
<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"
     viewBox="7 3 20 20">
  <rect x="7" y="3" width="20" height="20" fill="#c00000"/>
</svg>'''
    output = svg2svg(svg, output_width=25.5)
    assert b'width="25.5"' in output
    assert b'height="25.5"' in output
    assert b'viewBox="0 0 25.5 25.5"' in output
